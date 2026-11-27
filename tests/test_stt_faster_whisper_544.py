"""Tests for #544 (i.0.0.5.44): local faster-whisper STT service, STTProvider, and Worker execution."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from adapters.providers import FasterWhisperProvider, STTProvider, get_providers, provider_matrix
from core.outbound_only import inbound_port_violations
from services import stt_service
from worker.role_executor import execute_role_task

ROOT = Path(__file__).resolve().parents[1]


class _FakeWhisperModel:
    last_call: dict = {}

    def __init__(self, model_name, device="cpu", compute_type="int8", download_root=None):
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.download_root = download_root

    def transcribe(self, audio_path: str, language=None, vad_filter=True):
        data = Path(audio_path).read_bytes()
        _FakeWhisperModel.last_call = {
            "size": len(data),
            "language": language,
            "vad_filter": vad_filter,
            "model": self.model_name,
        }
        segments = [
            SimpleNamespace(start=0.0, end=1.45, text=" Привіт, Україно! "),
            SimpleNamespace(start=1.5, end=2.8, text=" Тест розпізнавання. "),
        ]
        info = SimpleNamespace(language=language or "uk", language_probability=0.9876, duration=2.8)
        return iter(segments), info


def test_stt_service_health_and_transcribe_uk_and_auto(monkeypatch):
    stt_service._model_cache.clear()
    monkeypatch.setattr(stt_service, "_whisper_model_class", lambda: _FakeWhisperModel)
    client = TestClient(stt_service.app)

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "HEALTHY"
    assert health.json()["engine"] == "faster-whisper"

    sample_wav = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32
    expected_sha = hashlib.sha256(sample_wav).hexdigest()
    b64 = base64.b64encode(sample_wav).decode("ascii")

    # Explicit Ukrainian language
    resp_uk = client.post(
        "/transcribe",
        json={"audio_base64": b64, "filename": "clip.wav", "language": "uk", "model": "small"},
    )
    assert resp_uk.status_code == 200
    body = resp_uk.json()
    assert body["format"] == "stt_contract/v1"
    assert body["engine"] == "faster-whisper"
    assert body["language"] == "uk"
    assert body["text"] == "Привіт, Україно! Тест розпізнавання."
    assert len(body["segments"]) == 2
    assert body["segments"][0]["start"] == 0.0
    assert body["audio_sha256"] == expected_sha
    assert _FakeWhisperModel.last_call["language"] == "uk"

    # Auto language detection
    resp_auto = client.post("/transcribe", json={"audio_base64": b64, "filename": "voice.ogg", "language": "auto"})
    assert resp_auto.status_code == 200
    assert resp_auto.json()["language"] == "uk"
    assert _FakeWhisperModel.last_call["language"] is None


def test_stt_service_fail_closed_validation_and_unavailable_backend(monkeypatch):
    stt_service._model_cache.clear()
    monkeypatch.setattr(stt_service, "_whisper_model_class", lambda: None)
    client = TestClient(stt_service.app)

    assert client.get("/health").status_code == 503
    sample_b64 = base64.b64encode(b"RIFFWAVE").decode("ascii")
    assert client.post("/transcribe", json={"audio_base64": sample_b64}).status_code == 503

    monkeypatch.setattr(stt_service, "_whisper_model_class", lambda: _FakeWhisperModel)
    assert client.post("/transcribe", json={"audio_base64": "!!!not-base64!!!"}).status_code == 422
    assert client.post("/transcribe", json={"audio_base64": sample_b64, "filename": "bad.exe"}).status_code == 422
    assert client.post("/transcribe", json={"audio_base64": sample_b64, "language": "12345"}).status_code == 422
    assert client.post("/transcribe", json={"audio_base64": sample_b64, "model": "../escape"}).status_code == 422


def test_faster_whisper_provider_and_registry(monkeypatch, tmp_path):
    audio_file = tmp_path / "sample.wav"
    audio_bytes = b"RIFF\x00\x00\x00\x00WAVE"
    audio_file.write_bytes(audio_bytes)
    expected_sha = hashlib.sha256(audio_bytes).hexdigest()

    monkeypatch.delenv("STT_URL", raising=False)
    assert FasterWhisperProvider().configured() is False

    monkeypatch.setenv("STT_URL", "http://stt:8099")
    reg = get_providers()
    prov = reg.stt("faster-whisper")
    assert isinstance(prov, STTProvider)
    assert isinstance(prov, FasterWhisperProvider)
    assert prov.configured() is True
    assert provider_matrix()["stt"]["backend"] == "faster-whisper"
    assert provider_matrix()["stt"]["configured"] is True

    captured = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "format": "stt_contract/v1",
                "engine": "faster-whisper",
                "model": "small",
                "language": "uk",
                "language_probability": 0.99,
                "duration": 1.5,
                "text": "Добрий день",
                "segments": [{"id": 0, "start": 0.0, "end": 1.5, "text": "Добрий день"}],
                "audio_sha256": expected_sha,
            }

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(httpx, "post", fake_post)
    result = prov.transcribe(audio_file, language="uk", model="small")
    assert captured["url"] == "http://stt:8099/transcribe"
    assert captured["json"]["language"] == "uk"
    assert result["text"] == "Добрий день"
    assert result["audio_sha256"] == expected_sha


def test_worker_executes_stt_through_execute_role_task_and_enforces_role_auth(monkeypatch):
    stt_service._model_cache.clear()
    monkeypatch.setattr(stt_service, "_whisper_model_class", lambda: _FakeWhisperModel)
    client = TestClient(stt_service.app)

    def fake_post(url, json=None, timeout=None):
        assert url.endswith("/transcribe")
        return client.post("/transcribe", json=json)

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setenv("STT_URL", "http://stt:8099")

    sample_wav = b"RIFF\x10\x00\x00\x00WAVEfmt " + b"\x01" * 16
    task = {
        "task": "stt",
        "scene_id": "scene-1",
        "filename": "scene-1.wav",
        "language": "uk",
        "audio_base64": base64.b64encode(sample_wav).decode("ascii"),
    }

    artifacts = execute_role_task("voice", task, _migration=True)
    assert len(artifacts) == 1
    art = artifacts[0]
    assert art["kind"] == "transcript"
    assert art["filename"] == "transcript-scene-1.json"
    assert art["contract"]["format"] == "stt_contract/v1"
    assert art["contract"]["engine"] == "faster-whisper"
    assert art["contract"]["audio_sha256"] == hashlib.sha256(sample_wav).hexdigest()
    parsed = json.loads(base64.b64decode(art["data_base64"]).decode("utf-8"))
    assert parsed["text"] == "Привіт, Україно! Тест розпізнавання."

    # Unauthorized role cannot execute stt
    with pytest.raises(PermissionError):
        execute_role_task("backup", task, _migration=True)


def test_stt_dockerfile_and_compose_profile_preserve_outbound_only():
    dockerfile = (ROOT / "docker" / "stt" / "Dockerfile").read_text(encoding="utf-8")
    assert "faster-whisper==" in dockerfile
    assert "uvicorn" in dockerfile
    assert "services.stt_service:app" in dockerfile

    compose_path = ROOT / "deploy" / "docker-compose.yml"
    compose_text = compose_path.read_text(encoding="utf-8")
    assert 'profiles: ["stt"]' in compose_text
    assert "http://localhost:8099/health" in compose_text
    # Ensure compose remains compliant with outbound-only policy
    violations = inbound_port_violations(compose_path)
    assert violations == [], f"Outbound-only violations: {violations}"

    # Release workflow, bootstrap, and runtime-env updater must ship stt in the signed image lock
    release_yml = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "name: stt" in release_yml and "docker/stt/Dockerfile" in release_yml
    bootstrap_sh = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
    assert "VERTEP_STT_IMAGE=$(optional_image stt)" in bootstrap_sh
    updater = (ROOT / "scripts" / "update-runtime-env.py").read_text(encoding="utf-8")
    assert '"stt": "VERTEP_STT_IMAGE"' in updater


def test_dispatcher_routes_stt_tasks_only_to_speech_recognition_workers():
    from datetime import datetime, timezone
    from core.dispatcher import available_worker
    from core.models import Job, JobStatus

    now = datetime.now(timezone.utc).isoformat()
    job = Job(
        job_id="job-stt-1",
        topic="transcribe",
        character_id="c1",
        priority=5,
        status=JobStatus.NEW,
        task_type="stt",
        created_at=now,
    )
    voice_only = {
        "node_name": "voice-only",
        "role": "voice",
        "status": "READY",
        "capabilities": ["speech_synthesis"],
        "tested_capabilities": ["speech_synthesis"],
        "supported_tasks": ["voice"],
        "last_seen": now,
        "self_test": {"status": "PASSED", "tested_at": now, "capabilities": ["speech_synthesis"]},
    }
    voice_with_stt = {
        "node_name": "voice-stt",
        "role": "voice",
        "status": "READY",
        "capabilities": ["speech_synthesis", "speech_recognition"],
        "tested_capabilities": ["speech_synthesis", "speech_recognition"],
        "supported_tasks": ["voice", "stt"],
        "last_seen": now,
        "self_test": {"status": "PASSED", "tested_at": now, "capabilities": ["speech_synthesis", "speech_recognition"]},
    }
    assert available_worker([voice_only], job, task_type="stt") is None
    assert available_worker([voice_only, voice_with_stt], job, task_type="stt")["node_name"] == "voice-stt"


def test_core_dispatches_and_persists_transcription(monkeypatch):
    from core.app import app
    from core.artifacts import register_artifact
    from core.state import store

    monkeypatch.setenv("NODE_API_TOKEN", "stt-integration-token")
    monkeypatch.setenv("REQUIRE_WORKER_SELF_TEST", "false")
    client = TestClient(app, headers={"x-vertep-token": "stt-integration-token"})
    job = store.create("Transcribe this job's audio", "did_samogon", 5)
    audio_path = store.root / job.job_id / "audio" / "input.wav"
    audio_path.write_bytes(b"RIFF\x10\x00\x00\x00WAVEfmt " + b"\x01" * 16)
    audio = register_artifact(job, store.root, audio_path, "audio")

    response = client.post(f"/api/jobs/{job.job_id}/transcriptions",
                           json={"artifact_id": audio.artifact_id, "language": "uk"})
    assert response.status_code == 202, response.text
    task_id = response.json()["task_id"]
    worker = {"node_name": "test-stt", "role": "voice", "vram_mb": 0,
              "capabilities": ["speech_recognition"], "supported_tasks": ["stt"]}
    assert client.post("/api/workers/heartbeat", json=worker).status_code == 200
    response = client.post("/api/tasks/claim", json=worker)
    assert response.status_code == 200, response.text
    task = response.json()["task"]
    assert task["task_id"] == task_id
    assert base64.b64decode(task["audio_base64"]) == audio_path.read_bytes()

    assert client.post("/api/tasks/renew", json={
        "node_name": "test-stt", "task_id": task_id,
    }).json() == {"renewed": True}
    stt_service._model_cache.clear()
    monkeypatch.setattr(stt_service, "_whisper_model_class", lambda: _FakeWhisperModel)
    runtime = TestClient(stt_service.app)
    monkeypatch.setattr(httpx, "post", lambda url, json, timeout: runtime.post("/transcribe", json=json))
    artifacts = execute_role_task("voice", task, _migration=True)
    body = {"job_id": job.job_id, "task_id": task_id, "success": True, "artifacts": artifacts}
    rejected = client.post("/api/tasks/result", json={**body, "node_name": "other-node"})
    assert rejected.status_code == 409
    response = client.post("/api/tasks/result", json={**body, "node_name": "test-stt"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "COMPLETED"
    artifact_id = response.json()["artifact_id"]
    transcript = client.get(f"/api/jobs/{job.job_id}/artifacts/{artifact_id}/download")
    assert transcript.json()["text"] == "Привіт, Україно! Тест розпізнавання."
