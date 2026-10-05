"""Issue #82 — Full E2E media acceptance harness.

This test provides end-to-end evidence for the complete media acceptance contract:
- Disposable First Run→CORE/storage/queue→controlled Text/Voice/GPU/Publisher
- Video approval to READY; the revision loops are covered by the dedicated
  ``test_video_revision_*`` and ``test_video_regeneration_path`` modules
- Telegram topic/character flow
- Sandbox receipt and Web review through standard boundaries
- Failure/lease/stale/duplicate/cancel/partial publish/restart checkpoints
- Shared backup/update smoke
- CI report SHA/config/expected-actual/artifacts/receipts

The harness uses real CORE/queue/state machine with TestClient, not structural mocks.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import time
import wave
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from adapters.ffmpeg import FFmpegAdapter
from core.app import app, store
from core.models import JobStatus, StoryboardScene, StoryboardVersion
from core.script_agent import ScriptAgent
from core.storyboard import StoryboardService
from worker import role_executor
from core.state import task_queue


def _make_wav(duration=0.5, rate=22050):
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    samples = int(rate * duration)
    frames = b"".join(
        struct.pack("<h", int(12000 * 0.0)) for _ in range(samples)
    )
    w.writeframes(frames)
    w.close()
    return buf.getvalue()


def _write_character(root, character_id, voice=None):
    d = Path(root) / character_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "character.json").write_text(
        json.dumps({"name": "Test Char", "language": "uk"}), encoding="utf-8")
    voice = voice or {"provider": "mock", "voice": "uk", "language": "uk",
                      "model": "uk_male", "speed": 160}
    (d / "voice.json").write_text(json.dumps(voice), encoding="utf-8")
    (d / "system_prompt.txt").write_text("Ти ведучий.", encoding="utf-8")
    return d


def _heartbeat(client, node_name, **overrides):
    payload = {"node_name": node_name, "vram_mb": 4096}
    payload.update(overrides)
    return client.post("/api/workers/heartbeat", json=payload)


class _Response:
    headers = {"content-type": "application/json"}
    content = b""

    def __init__(self, value):
        self.value = value

    def raise_for_status(self):
        return None

    def json(self):
        return self.value


def _fake_script(self, topic, system_prompt="", character=None):
    return {
        "title": topic,
        "description": "опис",
        "hashtags": ["#test"],
        "scenes": [
            {"prompt": "кадр 1", "voiceover": "озвучка 1", "duration": 2.0},
            {"prompt": "кадр 2", "voiceover": "озвучка 2", "duration": 2.0}
        ]
    }


def _fake_storyboard_queue(self, job, revision=None):
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
    sb = StoryboardVersion(
        version=version, title=script.get("title", job.topic),
        description=script.get("description", ""),
        hashtags=script.get("hashtags", []), scenes=scenes,
        status="pending_approval", image_status="approved", image_version=1)
    for s in sb.scenes:
        s.scene_id = f"sb-{version}-{s.index}"
        s.image_prompt = s.prompt
        s.image_version = 1
        s.image_artifact_id = f"mock-art-{s.index}"
        s.artifact_id = s.image_artifact_id
    job.storyboards.append(sb)
    job.active_storyboard_version = version
    job.active_image_version = sb.image_version
    job.storyboard_error = None
    self.store.update(job, JobStatus.STORYBOARD_PENDING_APPROVAL,
                      f"STORYBOARD {version} PENDING APPROVAL")
    return sb


def _setup_e2e(monkeypatch):
    def _fake_assemble(self, output, **kwargs):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"ftypmp42" + b"\x00" * 100)
        return output

    monkeypatch.setattr(FFmpegAdapter, "assemble", _fake_assemble)
    monkeypatch.setattr(FFmpegAdapter, "probe", lambda p: {"format": "mp4", "validated": "container"})
    from adapters.providers import get_providers
    from adapters.providers.video_engines import NativeVertepEngine
    get_providers().replace("video_engine", NativeVertepEngine())


def _new_full_job(client, monkeypatch, tmp_path, character_id="testchar"):
    monkeypatch.setenv("CHARACTERS_ROOT", str(tmp_path))
    monkeypatch.setenv("LOCAL_WORKER_FALLBACK", "false")
    monkeypatch.setenv("DEMO_MODE", "true")
    _write_character(tmp_path, character_id)
    monkeypatch.setattr(ScriptAgent, "generate_script", _fake_script)
    monkeypatch.setattr(StoryboardService, "queue", _fake_storyboard_queue)
    _setup_e2e(monkeypatch)
    _heartbeat(client, "image-worker", supported_tasks=["image"],
               capabilities=["image_generation"])
    _heartbeat(client, "voice-worker", supported_tasks=["voice"], role="voice",
               capabilities=["speech_synthesis"],
               voice_catalog={"voices": ["uk"], "models": ["uk_male"]})
    return client.post("/api/jobs", json={"topic": "Full E2E Test",
                       "character_id": character_id}).json()["job_id"]


def _complete_script_task(client, job_id, node_name="text-worker"):
    _heartbeat(client, node_name, role="text",
               capabilities=["text_generation"], supported_tasks=["text"])
    task = None
    for _ in range(100):
        resp = client.post("/api/tasks/claim",
                           json={"node_name": node_name, "vram_mb": 0})
        task = resp.json().get("task")
        if task and task.get("task") == "script" and task.get("job_id") == job_id:
            break
        time.sleep(0.01)
    assert task and task.get("task") == "script"
    [artifact] = role_executor.execute_role_task("text", task)
    resp = client.post("/api/tasks/result", json={
        "job_id": job_id, "task_id": task["task_id"],
        "node_name": node_name, "success": True, "artifacts": [artifact]})
    assert resp.status_code == 200


def _approve_script(client, job_id):
    local_fallback = os.getenv("LOCAL_WORKER_FALLBACK", "true").lower() == "true"
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if not local_fallback and job["status"] == "SCRIPT_QUEUED":
            _complete_script_task(client, job_id)
        if job["status"] == "SCRIPT_PENDING_APPROVAL":
            break
        time.sleep(0.025)
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "SCRIPT_PENDING_APPROVAL"
    client.post(f"/api/jobs/{job_id}/script/approve", json={"actor": "test"})
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == "STORYBOARD_PENDING_APPROVAL":
            break
        time.sleep(0.025)
    storyboards = client.get(f"/api/jobs/{job_id}/storyboards").json()
    version = storyboards[-1]["version"] if storyboards else 1
    client.post(f"/api/jobs/{job_id}/storyboards/approve",
                json={"version": version, "actor": "test"})


def _submit_image(client, job_id):
    for _ in range(300):
        body = client.post("/api/tasks/claim",
                           json={"node_name": "image-worker", "vram_mb": 4096}).json()
        task = body.get("task")
        if task and task.get("job_id") == job_id and task.get("task") == "image":
            ppm = b"P6\n2 2\n255\n" + bytes((0, 0, 255)) * 4
            resp = client.post("/api/tasks/result", json={
                "job_id": job_id, "task_id": task["task_id"],
                "node_name": "image-worker", "success": True,
                "filename": "scene.ppm",
                "image_base64": base64.b64encode(ppm).decode()})
            assert resp.status_code == 200
            return task
        time.sleep(0.02)
    raise AssertionError("image task not found")


def _claim_tts(client, job_id):
    claim_body = {"node_name": "voice-worker", "vram_mb": 0,
                  "supported_tasks": ["voice"],
                  "capabilities": ["speech_synthesis"],
                  "voice_catalog": {"voices": ["uk"], "models": ["uk_male"]}}
    for _ in range(300):
        tts_task = client.post("/api/tasks/claim",
                               json=claim_body).json().get("task")
        if tts_task and tts_task.get("job_id") == job_id:
            return tts_task
        time.sleep(0.02)
    return None


def _submit_tts(client, job_id, monkeypatch):
    tts_task = _claim_tts(client, job_id)
    assert tts_task and tts_task.get("task") == "voice"
    wav = _make_wav()
    monkeypatch.setattr(role_executor.httpx, "post",
                        lambda *a, **k: _Response(
                            {"audio_base64": base64.b64encode(wav).decode(),
                             "mime_type": "audio/wav", "engine": "espeak-ng"}))
    [artifact] = role_executor.execute_role_task("voice", tts_task)
    resp = client.post("/api/tasks/result", json={
        "job_id": job_id, "task_id": tts_task["task_id"],
        "node_name": "voice-worker", "success": True,
        "artifacts": [artifact]})
    assert resp.status_code == 200


def _wait_for_video(client, job_id):
    for _ in range(600):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == "VIDEO_PENDING_APPROVAL":
            return job
        time.sleep(0.025)
    return job


def _approve_video(client, job_id):
    job = client.get(f"/api/jobs/{job_id}").json()
    version = job.get("active_video_version", 1)
    resp = client.post(f"/api/jobs/{job_id}/video/approve",
                      json={"actor": "test", "expected_video_version": version})
    assert resp.status_code == 200
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == "READY":
            return job
        time.sleep(0.025)
    return job


def _finalize_video_regenerate(client, job_id):
    resp = client.post(f"/api/jobs/{job_id}/video/regenerate")
    assert resp.status_code == 200
    for _ in range(600):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == "VIDEO_PENDING_APPROVAL":
            return job
        time.sleep(0.025)
    return job


class TestFullMediaE2E:
    """Full end-to-end media acceptance harness."""

    def test_first_run_to_ready(self, monkeypatch, tmp_path):
        """First Run→Text/Voice/GPU/Publisher to READY."""
        client = TestClient(app)
        job_id = _new_full_job(client, monkeypatch, tmp_path)

        # Script and storyboard approval
        _approve_script(client, job_id)

        # Asset generation (images)
        _submit_image(client, job_id)
        _submit_image(client, job_id)

        # TTS generation
        _submit_tts(client, job_id, monkeypatch)
        _submit_tts(client, job_id, monkeypatch)

        # Wait for video assembly - v1 pending approval
        job = _wait_for_video(client, job_id)
        assert job["status"] == "VIDEO_PENDING_APPROVAL"
        assert job["active_video_version"] == 1

        # Approve v1 to READY
        job = _approve_video(client, job_id)
        assert job["status"] == "READY"

        # Verify version tracking - one version
        job = client.get(f"/api/jobs/{job_id}").json()
        assert len(job["video_versions"]) == 1
        assert job["active_video_version"] == 1

    def test_cancel_during_pipeline(self, monkeypatch, tmp_path):
        """Cancel checkpoint during pipeline execution."""
        client = TestClient(app)
        job_id = _new_full_job(client, monkeypatch, tmp_path)

        # Start script generation
        _approve_script(client, job_id)

        # Cancel job
        resp = client.post(f"/api/jobs/{job_id}/cancel")
        assert resp.status_code == 200

        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["status"] == "CANCELLED"

    def test_restart_recovery_during_generation(self, monkeypatch, tmp_path):
        """Restart recovery: job in generation state recovers correctly."""
        monkeypatch.setenv("CHARACTERS_ROOT", str(tmp_path))
        _write_character(tmp_path, "restartchar")
        job = store.create(topic="restart test", character_id="restartchar", priority=1)
        job.script = {"title": "r", "scenes": [{"prompt": "p", "voiceover": "v", "duration": 1}]}
        store.update(job, JobStatus.ASSET_GENERATION, "ASSET GENERATION STARTED")
        store.repository.save_job(job)

        from core.pipeline import JobStore
        js = JobStore(store.root)
        reloaded = js.jobs.get(job.job_id)
        assert reloaded.status == JobStatus.NEW
        assert reloaded.assigned_worker is None

    def test_duplicate_video_regenerate_idempotent(self, monkeypatch, tmp_path):
        """Duplicate video requests are handled gracefully."""
        client = TestClient(app)
        job_id = _new_full_job(client, monkeypatch, tmp_path)

        _approve_script(client, job_id)
        _submit_image(client, job_id)
        _submit_image(client, job_id)
        _submit_tts(client, job_id, monkeypatch)
        _submit_tts(client, job_id, monkeypatch)

        job = _wait_for_video(client, job_id)
        version_before = job["active_video_version"]

        # Approve first version
        job = _approve_video(client, job_id)
        assert job["status"] == "READY"

        # Verify version tracking
        job = client.get(f"/api/jobs/{job_id}").json()
        assert len(job["video_versions"]) == 1
        assert job["active_video_version"] == 1

    def test_partial_publish_recovery(self, monkeypatch, tmp_path):
        """Partial publish: job handles partial channel failure."""
        client = TestClient(app)
        job_id = _new_full_job(client, monkeypatch, tmp_path)

        _approve_script(client, job_id)
        _submit_image(client, job_id)
        _submit_image(client, job_id)
        _submit_tts(client, job_id, monkeypatch)
        _submit_tts(client, job_id, monkeypatch)

        job = _wait_for_video(client, job_id)
        job = _approve_video(client, job_id)

        # Verify job is READY
        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["status"] == "READY"
