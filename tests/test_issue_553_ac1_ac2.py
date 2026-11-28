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
