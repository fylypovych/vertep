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


def test_voices_endpoint_is_empty_when_no_voice_table():
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


def test_synthesize_accepts_supported_voice(monkeypatch):
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