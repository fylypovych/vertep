"""Acceptance tests for Issue i.0.0.0.72 — Voice artifact contract and end-to-end acceptance.

Task 1: production audio contract/checksum is mandatory; missing contract/SHA
        is rejected as incomplete proof.
Task 2: Controlled Text/Storyboard->Voice->audio->assembly with cancel/loss/
        reconnect/lease/duplicate/stale/restart; metadata and parameter impact.
"""

import base64
import hashlib
import json
import math
import os
import struct
import time
import wave
import io
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from adapters.ffmpeg import FFmpegAdapter
from core.app import app, store
from core.api.job_helpers import _persist_tts_contract, _tts_task_for
from core.models import JobStatus, StoryboardScene, StoryboardVersion
from core.script_agent import ScriptAgent
from core.storyboard import StoryboardService

from worker import role_executor


def _make_wav(duration=0.5, rate=22050):
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    samples = int(rate * duration)
    frames = b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / rate)))
        for i in range(samples))
    w.writeframes(frames)
    w.close()
    return buf.getvalue()


def _write_character(root, character_id, voice=None):
    from pathlib import Path
    d = Path(root) / character_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "character.json").write_text(
        json.dumps({"name": "Voice Char", "language": "uk"}), encoding="utf-8")
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
    return {"title": topic, "description": "опис", "hashtags": ["#t"],
            "scenes": [{"prompt": "кадр", "voiceover": "озвучка", "duration": 1}]}


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


def _new_voice_job(client, monkeypatch, tmp_path, character_id="voicechar"):
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
    return client.post("/api/jobs", json={"topic": "Acceptance",
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


def _wait_for(client, job_id, statuses=("READY", "FAILED")):
    for _ in range(600):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == "VIDEO_PENDING_APPROVAL":
            client.post(f"/api/jobs/{job_id}/video/approve",
                        json={"actor": "test"})
            continue
        if job["status"] in statuses:
            return job
        time.sleep(0.025)
    return job


# ---------------------------------------------------------------------------
# Task 1: Contract mandatory
# ---------------------------------------------------------------------------

class TestContractMandatory:

    def test_reject_tts_result_without_contract(self, monkeypatch, tmp_path):
        client = TestClient(app)
        job_id = _new_voice_job(client, monkeypatch, tmp_path)
        _approve_script(client, job_id)
        _submit_image(client, job_id)
        tts_task = _claim_tts(client, job_id)
        assert tts_task and tts_task.get("task") == "voice"
        wav = _make_wav()
        artifact = {"filename": "voice.wav", "kind": "audio",
                    "data_base64": base64.b64encode(wav).decode()}
        resp = client.post("/api/tasks/result", json={
            "job_id": job_id, "task_id": tts_task["task_id"],
            "node_name": "voice-worker", "success": True,
            "artifacts": [artifact]})
        assert resp.status_code == 400
        assert "contract" in resp.json()["detail"].lower()

    def test_reject_tts_result_with_empty_contract(self, monkeypatch, tmp_path):
        client = TestClient(app)
        job_id = _new_voice_job(client, monkeypatch, tmp_path)
        _approve_script(client, job_id)
        _submit_image(client, job_id)
        tts_task = _claim_tts(client, job_id)
        wav = _make_wav()
        artifact = {"filename": "voice.wav", "kind": "audio",
                    "data_base64": base64.b64encode(wav).decode(),
                    "contract": {}}
        resp = client.post("/api/tasks/result", json={
            "job_id": job_id, "task_id": tts_task["task_id"],
            "node_name": "voice-worker", "success": True,
            "artifacts": [artifact]})
        assert resp.status_code == 400

    def test_reject_tts_result_with_tampered_sha256(self, monkeypatch, tmp_path):
        client = TestClient(app)
        job_id = _new_voice_job(client, monkeypatch, tmp_path)
        _approve_script(client, job_id)
        _submit_image(client, job_id)
        tts_task = _claim_tts(client, job_id)
        wav = _make_wav()
        contract = {"format": "audio_contract/v1", "provider": "mock",
                    "voice": "uk", "sha256": hashlib.sha256(b"wrong").hexdigest(),
                    "text_sha256": hashlib.sha256(b"t").hexdigest(),
                    "engine": "espeak-ng", "language": "uk",
                    "mime_type": "audio/wav", "size": len(wav),
                    "model": None, "duration": None,
                    "scene_id": None, "character_id": None}
        artifact = {"filename": "voice.wav", "kind": "audio",
                    "data_base64": base64.b64encode(wav).decode(),
                    "contract": contract}
        resp = client.post("/api/tasks/result", json={
            "job_id": job_id, "task_id": tts_task["task_id"],
            "node_name": "voice-worker", "success": True,
            "artifacts": [artifact]})
        assert resp.status_code == 400
        assert "sha256" in resp.json()["detail"].lower()

    def test_reject_tts_result_without_provider_voice(self, monkeypatch, tmp_path):
        client = TestClient(app)
        job_id = _new_voice_job(client, monkeypatch, tmp_path)
        _approve_script(client, job_id)
        _submit_image(client, job_id)
        tts_task = _claim_tts(client, job_id)
        wav = _make_wav()
        contract = {"format": "audio_contract/v1",
                    "sha256": hashlib.sha256(wav).hexdigest()}
        artifact = {"filename": "voice.wav", "kind": "audio",
                    "data_base64": base64.b64encode(wav).decode(),
                    "contract": contract}
        resp = client.post("/api/tasks/result", json={
            "job_id": job_id, "task_id": tts_task["task_id"],
            "node_name": "voice-worker", "success": True,
            "artifacts": [artifact]})
        assert resp.status_code == 400

    def test_persist_contract_rejects_non_dict(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CHARACTERS_ROOT", str(tmp_path))
        _write_character(tmp_path, "vc")
        job = store.create(topic="t", character_id="vc", priority=1)
        job.script = {"title": "t", "scenes": [{"prompt": "p", "voiceover": "v", "duration": 1}]}
        from core.orchestration import initialize_plan
        initialize_plan(job)
        scene = store.jobs[job.job_id].scenes[0]
        audio_path = store.root / job.job_id / "audio" / "test.wav"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"wav-data")
        result = SimpleNamespace(task_id="t1", node_name="vw")
        with pytest.raises(ValueError, match="missing or not a dict"):
            _persist_tts_contract(store, job, scene, result,
                                  audio_path, b"wav-data", "not-a-dict")

    def test_persist_contract_rejects_missing_sha256(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CHARACTERS_ROOT", str(tmp_path))
        _write_character(tmp_path, "vc")
        job = store.create(topic="t", character_id="vc", priority=1)
        job.script = {"title": "t", "scenes": [{"prompt": "p", "voiceover": "v", "duration": 1}]}
        from core.orchestration import initialize_plan
        initialize_plan(job)
        scene = store.jobs[job.job_id].scenes[0]
        audio_path = store.root / job.job_id / "audio" / "test.wav"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"wav-data")
        result = SimpleNamespace(task_id="t1", node_name="vw")
        contract = {"provider": "mock", "voice": "uk"}
        with pytest.raises(ValueError, match="sha256"):
            _persist_tts_contract(store, job, scene, result,
                                  audio_path, b"wav-data", contract)

    def test_worker_contract_v1_fields(self, monkeypatch):
        audio = b"RIFF\x04\x00\x00\x00WAVE"
        monkeypatch.setattr(role_executor.httpx, "post",
                            lambda *a, **k: _Response(
                                {"audio_base64": base64.b64encode(audio).decode(),
                                 "mime_type": "audio/wav", "engine": "espeak-ng"}))
        [artifact] = role_executor.execute_role_task("voice", {
            "task": "voice", "topic": "привіт", "provider": "mock",
            "voice": "uk", "language": "uk", "model": "uk_male",
            "scene_id": "sc-1", "character_id": "ch-1", "speed": 160})
        c = artifact["contract"]
        assert c["format"] == "audio_contract/v1"
        assert c["provider"] == "mock"
        assert c["voice"] == "uk"
        assert c["language"] == "uk"
        assert c["model"] == "uk_male"
        assert c["scene_id"] == "sc-1"
        assert c["character_id"] == "ch-1"
        assert c["sha256"] == hashlib.sha256(audio).hexdigest()
        assert c["size"] == len(audio)
        assert "text_sha256" in c


# ---------------------------------------------------------------------------
# Task 2: Controlled TTS lifecycle
# ---------------------------------------------------------------------------

class TestTTSCancelPause:

    def test_cancel_job_during_tts(self, monkeypatch, tmp_path):
        client = TestClient(app)
        job_id = _new_voice_job(client, monkeypatch, tmp_path)
        _approve_script(client, job_id)
        _submit_image(client, job_id)
        tts_task = _claim_tts(client, job_id)
        assert tts_task
        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["status"] == "TTS_GENERATING"
        resp = client.post(f"/api/jobs/{job_id}/cancel")
        assert resp.status_code == 200
        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["status"] == "CANCELLED"

    def test_pause_job_during_tts(self, monkeypatch, tmp_path):
        client = TestClient(app)
        job_id = _new_voice_job(client, monkeypatch, tmp_path)
        _approve_script(client, job_id)
        _submit_image(client, job_id)
        tts_task = _claim_tts(client, job_id)
        assert tts_task
        resp = client.post(f"/api/jobs/{job_id}/pause")
        assert resp.status_code == 200
        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["status"] == "PAUSED"


class TestTTSDuplicateResult:

    def test_duplicate_tts_result_ignored(self, monkeypatch, tmp_path):
        client = TestClient(app)
        job_id = _new_voice_job(client, monkeypatch, tmp_path)
        _approve_script(client, job_id)
        _submit_image(client, job_id)
        tts_task = _claim_tts(client, job_id)
        assert tts_task
        monkeypatch.setattr(role_executor.httpx, "post",
                            lambda *a, **k: _Response(
                                {"audio_base64": base64.b64encode(_make_wav()).decode(),
                                 "mime_type": "audio/wav", "engine": "espeak-ng"}))
        [artifact] = role_executor.execute_role_task("voice", tts_task)
        resp1 = client.post("/api/tasks/result", json={
            "job_id": job_id, "task_id": tts_task["task_id"],
            "node_name": "voice-worker", "success": True,
            "artifacts": [artifact]})
        assert resp1.status_code == 200
        resp2 = client.post("/api/tasks/result", json={
            "job_id": job_id, "task_id": tts_task["task_id"],
            "node_name": "voice-worker", "success": True,
            "artifacts": [artifact]})
        assert resp2.status_code == 200


class TestTTSWorkerLossRecovery:

    def test_stale_voice_worker_requeues_tts(self, monkeypatch, tmp_path):
        from core.api.job_helpers import _recover_stale_workers
        client = TestClient(app)
        job_id = _new_voice_job(client, monkeypatch, tmp_path)
        _approve_script(client, job_id)
        _submit_image(client, job_id)
        tts_task = _claim_tts(client, job_id)
        assert tts_task
        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["status"] == "TTS_GENERATING"
        worker = store.workers.get("voice-worker")
        assert worker
        worker["last_seen"] = "2000-01-01T00:00:00+00:00"
        worker["current_job"] = job_id
        worker["current_task"] = tts_task["task_id"]
        store.save_worker(worker)
        _recover_stale_workers()
        worker = store.workers.get("voice-worker")
        assert worker["status"] == "OFFLINE"
        job_obj = store.jobs.get(job_id)
        assert tts_task["task_id"] not in job_obj.tts_active_task_ids


class TestTTSRestartRecovery:

    def test_tts_generating_recovers_to_new(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CHARACTERS_ROOT", str(tmp_path))
        _write_character(tmp_path, "vc")
        job = store.create(topic="restart", character_id="vc", priority=1)
        job.script = {"title": "r", "scenes": [{"prompt": "p", "duration": 1}]}
        store.update(job, JobStatus.TTS_GENERATING, "TTS GENERATION STARTED")
        store.repository.save_job(job)
        from core.pipeline import JobStore
        js = JobStore(store.root)
        reloaded = js.jobs.get(job.job_id)
        assert reloaded.status == JobStatus.NEW
        assert reloaded.tts_active_task_ids == {}
        assert reloaded.assigned_worker is None

    def test_tts_ready_recovers_to_new(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CHARACTERS_ROOT", str(tmp_path))
        _write_character(tmp_path, "vc")
        job = store.create(topic="restart2", character_id="vc", priority=1)
        job.script = {"title": "r", "scenes": [{"prompt": "p", "duration": 1}]}
        store.update(job, JobStatus.TTS_READY, "TTS COMPLETED")
        store.repository.save_job(job)
        from core.pipeline import JobStore
        js = JobStore(store.root)
        reloaded = js.jobs.get(job.job_id)
        assert reloaded.status == JobStatus.NEW


class TestTTSTaskMetadataFlow:

    def test_voice_config_flows_through_to_task(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CHARACTERS_ROOT", str(tmp_path))
        voice = {"provider": "kokoro", "voice": "af_heart",
                 "language": "en", "model": "tts-1", "speed": 180}
        _write_character(tmp_path, "vc", voice=voice)
        job = store.create(topic="meta", character_id="vc", priority=1)
        job.script = {"title": "m", "scenes": [
            {"prompt": "p", "voiceover": "hello world", "duration": 1}]}
        initialize_plan = __import__("core.orchestration", fromlist=["initialize_plan"]).initialize_plan
        initialize_plan(job)
        scene = job.scenes[0]
        task = _tts_task_for(job, scene)
        assert task["task"] == "voice"
        assert task["provider"] == "kokoro"
        assert task["voice"] == "af_heart"
        assert task["language"] == "en"
        assert task["model"] == "tts-1"
        assert task["speed"] == 180
        assert task["topic"] == "hello world"
        assert task["character_id"] == "vc"
        assert task["scene_id"] == scene.scene_id

    def test_disabled_provider_rejects(self, monkeypatch):
        monkeypatch.setattr(role_executor.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")))
        try:
            role_executor.execute_role_task("voice", {
                "task": "voice", "topic": "x", "provider": "disabled"})
        except RuntimeError as e:
            assert "disabled" in str(e)
        else:
            raise AssertionError("disabled provider accepted")
