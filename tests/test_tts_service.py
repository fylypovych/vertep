"""Tests for the local espeak-ng TTS runtime (services/tts_service.py).

Issue i.0.0.1.5: the espeak service previously exposed only text/voice/speed and
had no /voices endpoint, so CORE could never reconcile voice/provider/model/
language readiness with the actual runtime.  These tests cover the new /voices
catalog and the fail-closed rejection of unsupported voice identifiers.
"""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import services.tts_service as tts_service
from services.tts_service import app


def _client_with_voices(voices):
    """Return a TestClient whose runtime advertises exactly ``voices``."""
    tts_service.VOICES = list(voices)
    return TestClient(app)


def test_health_reports_espeak_engine(monkeypatch):
    monkeypatch.setattr(tts_service.shutil, "which", lambda name: "/usr/bin/espeak-ng")
    client = _client_with_voices([])
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["engine"] == "espeak-ng"


def test_health_is_503_when_espeak_missing(monkeypatch):
    monkeypatch.setattr(tts_service.shutil, "which", lambda name: None)
    client = _client_with_voices([])
    response = client.get("/health")
    assert response.status_code == 503


def test_voices_endpoint_lists_installed_voices():
    client = _client_with_voices(["uk", "en", "en-us"])
    response = client.get("/voices")
    assert response.status_code == 200
    body = response.json()
    assert body["engine"] == "espeak-ng"
    assert body["models"] == ["espeak-ng"]
    ids = {item["id"] for item in body["voices"]}
    assert ids == {"uk", "en", "en-us"}
    assert all(item["engine"] == "espeak-ng" for item in body["voices"])
    # Issue i.0.0.1.5: the catalog must reconcile voice/provider/model/
    # language/speed readiness with the stock espeak runtime.
    assert set(body["languages"]) == {"uk", "en"}
    # Issue #198/#165.1: the advertised catalog is a readiness contract, so it
    # must list ONLY the real espeak-ng runtime.  "mock" and "none" are not
    # synthesizers — advertising them would let CORE's fail-closed catalog gate
    # accept a voice task the worker could never execute.
    assert body["providers"] == ["espeak-ng"]
    assert body["speed_range"] == {"min": 80, "max": 450}


def test_voices_endpoint_is_empty_when_no_voice_table(monkeypatch):
    # With a real binary available, the stock CLI is now an authoritative
    # catalog source too. This case requires both sources to be unavailable.
    monkeypatch.setattr(tts_service.shutil, "which", lambda name: None)
    client = _client_with_voices([])
    response = client.get("/voices")
    assert response.status_code == 200
    assert response.json()["voices"] == []


def test_synthesize_rejects_unsupported_voice():
    client = _client_with_voices(["uk", "en"])
    response = client.post("/synthesize", json={"text": "привіт", "voice": "de"})
    assert response.status_code == 422
    assert "de" in response.json()["detail"]


def test_synthesize_rejects_malformed_voice_identifier():
    client = _client_with_voices(["uk"])
    response = client.post("/synthesize",
                           json={"text": "привіт", "voice": "bad voice with spaces"})
    assert response.status_code == 422


def test_synthesize_rejects_unsupported_language():
    client = _client_with_voices(["uk", "en"])
    response = client.post("/synthesize", json={"text": "привіт", "voice": "uk", "language": "de"})
    assert response.status_code == 422
    assert "de" in response.json()["detail"]


def test_synthesize_accepts_supported_language(monkeypatch):
    client = _client_with_voices(["uk", "en-us"])
    monkeypatch.setattr(tts_service.shutil, "which", lambda name: "/usr/bin/espeak-ng")

    class _Result:
        stdout = b"RIFF\x04\x00\x00\x00WAVE"
        returncode = 0

    monkeypatch.setattr(tts_service.subprocess, "run",
                         lambda *args, **kwargs: _Result())
    response = client.post("/synthesize", json={"text": "hi", "voice": "en-us", "language": "en"})
    assert response.status_code == 200


def test_synthesize_accepts_extra_provider_model_fields(monkeypatch):
    client = _client_with_voices(["uk"])
    monkeypatch.setattr(tts_service.shutil, "which", lambda name: "/usr/bin/espeak-ng")

    class _Result:
        stdout = b"RIFF\x04\x00\x00\x00WAVE"
        returncode = 0

    monkeypatch.setattr(tts_service.subprocess, "run",
                        lambda *args, **kwargs: _Result())
    response = client.post("/synthesize", json={"text": "привіт", "voice": "uk"})
    assert response.status_code == 200
    body = response.json()
    assert body["voice"] == "uk"
    assert body["engine"] == "espeak-ng"
    assert body["mime_type"] == "audio/wav"
    assert body["audio_base64"]


def test_synthesize_rejects_non_riff_output(monkeypatch):
    client = _client_with_voices(["uk"])
    monkeypatch.setattr(tts_service.shutil, "which", lambda name: "/usr/bin/espeak-ng")

    class _Result:
        stdout = b"<html>error</html>"
        returncode = 0

    monkeypatch.setattr(tts_service.subprocess, "run",
                        lambda *args, **kwargs: _Result())
    response = client.post("/synthesize", json={"text": "привіт", "voice": "uk"})
    assert response.status_code == 502


def test_synthesize_rejects_engine_failure(monkeypatch):
    client = _client_with_voices(["uk"])
    monkeypatch.setattr(tts_service.shutil, "which", lambda name: "/usr/bin/espeak-ng")

    def _fail(*args, **kwargs):
        raise tts_service.subprocess.CalledProcessError(1, "espeak-ng")

    monkeypatch.setattr(tts_service.subprocess, "run", _fail)
    response = client.post("/synthesize", json={"text": "привіт", "voice": "uk"})
    assert response.status_code == 502


# ---------------------------------------------------------------------------
# #455: readable voice names and the engine default voice
# ---------------------------------------------------------------------------

ESPEAK_VOICES = """Pty Language       Age/Gender VoiceName          File                 Other Languages
 5  en-us           --/M      English_(America)  gmw/en-US            (en 10)
 5  uk              --/M      Ukrainian          zle/uk
 5  mb-en1          --/M      en1                mb/mb-en1
"""


def test_voice_table_from_espeak_carries_readable_names(monkeypatch):
    monkeypatch.setattr(tts_service, "_VOICE_DATA_DIR", "/nonexistent")
    monkeypatch.setattr(tts_service.shutil, "which", lambda name: "/usr/bin/espeak-ng")
    monkeypatch.setattr(tts_service.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout=ESPEAK_VOICES, returncode=0))
    voices = tts_service._available_voices()
    assert voices == ["en-us", "uk"]  # MBROLA is a separate engine
    client = _client_with_voices(voices)
    body = client.get("/voices").json()
    assert body["voices"][1] == {"id": "uk", "name": "Ukrainian", "engine": "espeak-ng"}
    assert body["voices"][0]["name"] == "English (America)"
    assert body["default_voice"] == "default"


def test_default_voice_uses_the_engine_default_without_v(monkeypatch):
    monkeypatch.setattr(tts_service.shutil, "which", lambda name: "/usr/bin/espeak-ng")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=b"RIFF....WAVE", returncode=0)

    monkeypatch.setattr(tts_service.subprocess, "run", run)
    client = _client_with_voices(["uk"])
    response = client.post("/synthesize", json={"text": "OK", "voice": "default", "language": "uk"})
    assert response.status_code == 200
    assert "-v" not in calls[0]
    assert client.post("/synthesize", json={"text": "OK", "voice": "uk", "speed": 150}).status_code == 200
    assert calls[1][calls[1].index("-v") + 1] == "uk"
    assert client.post("/synthesize", json={"text": "OK", "voice": "nope"}).status_code == 422
