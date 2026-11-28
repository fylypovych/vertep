"""#553 AC1 (валідний WAV) та AC2 (Backup health)."""

import io
import struct
import wave

import pytest
from fastapi import HTTPException

from core import health_checks
from services.tts_service import normalize_wav


def _wav(frames: int = 2205, rate: int = 22050) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x10\x00" * frames)
    return buffer.getvalue()


def _streamed(data: bytes) -> bytes:
    """espeak-ng --stdout: the header carries a placeholder instead of the real sizes."""
    broken = bytearray(data)
    struct.pack_into("<I", broken, 4, 0x7FFFF024)
    struct.pack_into("<I", broken, 40, 0x7FFFF000)
    return bytes(broken)


def _duration(data: bytes) -> float:
    with wave.open(io.BytesIO(data)) as handle:
        return handle.getnframes() / handle.getframerate()


def test_streamed_header_is_rewritten_to_the_real_pcm_length():
    good = _wav()
    fixed = normalize_wav(_streamed(good))
    assert fixed == good
    assert struct.unpack_from("<I", fixed, 4)[0] == len(fixed) - 8
    assert struct.unpack_from("<I", fixed, 40)[0] == len(fixed) - 44
    assert _duration(fixed) == pytest.approx(0.1)


def test_valid_wav_is_unchanged_and_garbage_is_refused():
    good = _wav()
    assert normalize_wav(good) == good
    for bad in (b"", b"RIFF", b"not a wav at all, definitely", good[:30]):
        with pytest.raises(HTTPException) as error:
            normalize_wav(bad)
        assert error.value.status_code == 502


def test_wav_without_audio_frames_is_refused():
    with pytest.raises(HTTPException):
        normalize_wav(_wav(frames=0))


def test_espeak_synthesis_returns_a_decodable_wav(monkeypatch):
    import base64
    import services.tts_service as tts
    from fastapi.testclient import TestClient

    monkeypatch.setattr(tts.shutil, "which", lambda name: "/usr/bin/espeak-ng")
    tts.VOICES = ["uk", "en-us"]
    monkeypatch.setattr(tts.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": _streamed(_wav())})())
    response = TestClient(tts.app).post("/synthesize", json={"text": "привіт", "voice": "uk"})
    assert response.status_code == 200, response.text
    audio = base64.b64decode(response.json()["audio_base64"])
    assert _duration(audio) == pytest.approx(0.1)


class _Backup:
    def __init__(self, ok):
        self.ok = ok


def test_backup_is_not_ready_when_the_service_health_is_not_200(tmp_path, monkeypatch):
    monkeypatch.setattr(health_checks, "_http_get", lambda url, timeout=5: (False, "health_endpoint_unavailable"))
    ok, detail = health_checks.check_backup(str(tmp_path), "http://backup-service:8092")
    assert ok is False and detail == "health_backup_service_unavailable"


def test_backup_is_ready_when_directory_and_service_are_healthy(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(health_checks, "_http_get", lambda url, timeout=5: (seen.append(url) or True, "HTTP 200"))
    ok, detail = health_checks.check_backup(str(tmp_path), "http://backup-service:8092/")
    assert ok is True and seen == ["http://backup-service:8092/health"]


def test_backup_without_service_url_keeps_the_directory_check(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKUP_URL", raising=False)
    monkeypatch.setattr(health_checks, "_http_get", lambda *a, **k: pytest.fail("no service to ask"))
    assert health_checks.check_backup(str(tmp_path))[0] is True


def test_health_is_unhealthy_when_only_the_service_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_URL", "http://backup-service:8092")
    monkeypatch.setenv("BACKUP_ROOT", str(tmp_path))
    monkeypatch.setattr(health_checks, "_http_get", lambda url, timeout=5: (False, "health_endpoint_unavailable"))
    checks = {"role": "backup", "required": ["backup"], "backup": health_checks.check_backup()}
    assert health_checks.health_status(checks) == "UNHEALTHY"


def test_tts_backend_none_fails_voice_readiness_and_strips_tested_capability(monkeypatch):
    from datetime import datetime, timezone
    from fastapi.testclient import TestClient
    from core.app import app
    from core.state import store
    from worker.service import role_self_test

    monkeypatch.setenv("TTS_PROVIDER", "none")
    monkeypatch.setenv("NODE_API_TOKEN", "test-worker-token-553")
    monkeypatch.setenv("NODE_MTLS_REQUIRED", "false")
    store.workers.clear()

    ok, detail = health_checks.check_tts("http://tts:8090/health")
    assert ok is False and "none" in detail

    self_test = role_self_test("voice", {})
    assert self_test["status"] == "FAILED" and "disabled" in self_test["error"]

    core_test = role_self_test("core", {})
    assert "voice_disabled" not in core_test.get("evidence", {})  # no additional_roles=voice here

    monkeypatch.setenv("NODE_ADDITIONAL_ROLES", "voice")
    core_voice_test = role_self_test("core", {})
    assert core_voice_test["status"] == "PASSED"
    assert "voice_disabled" in core_voice_test["evidence"]

    payload = {
        "node_name": "voice-node-553",
        "role": "core",
        "status": "READY",
        "capabilities": ["speech_synthesis", "text_generation"],
        "self_test": {
            "status": "PASSED",
            "role": "core",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "evidence": {"voice_disabled": "TTS_PROVIDER=none"},
        },
        "executor_config": {"TTS_PROVIDER": "none"},
    }
    resp = TestClient(app).post(
        "/api/workers/heartbeat",
        json=payload,
        headers={"x-vertep-token": "test-worker-token-553"},
    )
    assert resp.status_code == 200, resp.text
    saved = next(w for w in store.load_workers() if w["node_name"] == "voice-node-553")
    assert "speech_synthesis" not in saved["tested_capabilities"]


def test_synthesize_endpoint_returns_valid_wav_through_voice_worker(monkeypatch):
    import base64
    from fastapi.testclient import TestClient
    from core.app import app
    from core.api import models as models_api
    import services.tts_service as tts

    monkeypatch.setattr(tts.shutil, "which", lambda name: "/usr/bin/espeak-ng")
    tts.VOICES = ["uk", "en-us"]
    monkeypatch.setattr(tts.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": _streamed(_wav())})())

    def worker_synth(text, voice="default", speed=150, **kwargs):
        res = TestClient(tts.app).post("/synthesize", json={"text": text, "voice": voice, "speed": speed, **kwargs})
        assert res.status_code == 200
        return base64.b64decode(res.json()["audio_base64"])

    monkeypatch.setattr(models_api, "synthesize_through_worker", worker_synth)
    response = TestClient(app).post("/api/models/voices/synthesize", json={"text": "Тест", "voice": "uk"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/wav")
    body = response.content
    assert struct.unpack_from("<I", body, 4)[0] == len(body) - 8
    assert struct.unpack_from("<I", body, 40)[0] == len(body) - 44
    assert _duration(body) == pytest.approx(0.1)
