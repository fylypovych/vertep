"""Voice preview negative paths — Issue i.0.0.0.78 remainder.

The preview endpoint must reject bad requests (422), disabled/unconfigured
TTS backends (409) and unreachable/unreadable TTS runtimes (502), and only
return audio for a fully configured backend.
"""

import base64
import math
import struct
import wave
import io

import httpx
import pytest
from fastapi.testclient import TestClient

from core.app import app
import core.api.models as models_api


@pytest.fixture
def client():
    return TestClient(app)


def _wav():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        frames = b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / 22050)))
            for i in range(4410))
        w.writeframes(frames)
    return buf.getvalue()


def _post(client, **payload):
    return client.post("/api/models/voices/preview", json=payload)


class TestPreviewValidation:
    def test_empty_text_is_rejected(self, client):
        assert _post(client, text="   ").status_code == 422
        assert _post(client).status_code == 422

    def test_text_over_limit_is_rejected(self, client):
        response = _post(client, text="x" * 501)
        assert response.status_code == 422
        assert "500" in response.json()["detail"]

    def test_non_integer_speed_is_rejected(self, client):
        assert _post(client, text="hi", speed="fast").status_code == 422

    @pytest.mark.parametrize("speed", [0, -1, 501, 9999])
    def test_out_of_range_speed_is_rejected(self, client, speed):
        response = _post(client, text="hi", speed=speed)
        assert response.status_code == 422
        assert "speed" in response.json()["detail"]


class TestPreviewBackendGates:
    def test_disabled_backend_is_409(self, client, monkeypatch):
        monkeypatch.setenv("TTS_PROVIDER", "none")
        response = _post(client, text="hi")
        assert response.status_code == 409
        assert "disabled" in response.json()["detail"]

    def test_empty_backend_is_409(self, client, monkeypatch):
        monkeypatch.setenv("TTS_PROVIDER", "")
        assert _post(client, text="hi").status_code == 409

    def test_unconfigured_backend_is_409(self, client, monkeypatch):
        monkeypatch.setenv("TTS_PROVIDER", "kokoro")
        monkeypatch.delenv("KOKORO_MODEL", raising=False)
        response = _post(client, text="hi")
        assert response.status_code == 409
        assert "configured" in response.json()["detail"]

    def test_gate_runs_before_runtime_calls(self, client, monkeypatch):
        monkeypatch.setenv("TTS_PROVIDER", "none")

        def _boom(*_args, **_kwargs):
            raise AssertionError("runtime must not be touched when disabled")

        monkeypatch.setattr(models_api, "list_voices", _boom)
        monkeypatch.setattr(models_api, "synthesize_voice", _boom)
        assert _post(client, text="hi").status_code == 409


class TestPreviewRuntimeFailures:
    def test_unreachable_runtime_is_502(self, client, monkeypatch):
        monkeypatch.setenv("TTS_PROVIDER", "mock")

        def _raise(*_args, **_kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(models_api, "list_voices", _raise)
        response = _post(client, text="hi")
        assert response.status_code == 502
        assert "unreachable" in response.json()["detail"]

    def test_unreadable_catalog_is_502(self, client, monkeypatch):
        monkeypatch.setenv("TTS_PROVIDER", "mock")

        def _raise(*_args, **_kwargs):
            raise ValueError("malformed payload")

        monkeypatch.setattr(models_api, "list_voices", _raise)
        response = _post(client, text="hi")
        assert response.status_code == 502
        assert "unreadable" in response.json()["detail"]

    def test_synthesis_failure_is_502(self, client, monkeypatch):
        monkeypatch.setenv("TTS_PROVIDER", "mock")
        monkeypatch.setattr(models_api, "list_voices", lambda: [])

        def _raise(*_args, **_kwargs):
            raise httpx.ConnectError("tts down")

        monkeypatch.setattr(models_api, "synthesize_voice", _raise)
        assert _post(client, text="hi").status_code == 502

    def test_empty_audio_is_502(self, client, monkeypatch):
        monkeypatch.setenv("TTS_PROVIDER", "mock")
        monkeypatch.setattr(models_api, "list_voices", lambda: [])
        monkeypatch.setattr(models_api, "synthesize_voice", lambda *a, **k: b"")
        assert _post(client, text="hi").status_code == 502


class TestPreviewVoices:
    def test_unknown_voice_is_422(self, client, monkeypatch):
        monkeypatch.setenv("TTS_PROVIDER", "mock")
        monkeypatch.setattr(models_api, "list_voices", lambda: [{"name": "uk"}])
        monkeypatch.setattr(
            models_api, "synthesize_voice",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no synthesis")))
        response = _post(client, text="hi", voice="nope")
        assert response.status_code == 422
        assert "Unknown voice" in response.json()["detail"]

    def test_known_voice_reaches_synthesis(self, client, monkeypatch):
        monkeypatch.setenv("TTS_PROVIDER", "mock")
        monkeypatch.setattr(models_api, "list_voices", lambda: [{"name": "uk"}])
        seen = {}

        def _synth(text, voice, speed):
            seen.update(text=text, voice=voice, speed=speed)
            return _wav()

        monkeypatch.setattr(models_api, "synthesize_voice", _synth)
        response = _post(client, text="привіт", voice="uk", speed=170)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("audio/")
        assert len(response.content) > 44
        assert seen == {"text": "привіт", "voice": "uk", "speed": 170}

    def test_default_voice_used_when_unspecified(self, client, monkeypatch):
        monkeypatch.setenv("TTS_PROVIDER", "mock")
        monkeypatch.setattr(models_api, "list_voices", lambda: [])
        seen = {}

        def _synth(text, voice, speed):
            seen.update(voice=voice, speed=speed)
            return b"RIFF\x04\x00\x00\x00WAVE"

        monkeypatch.setattr(models_api, "synthesize_voice", _synth)
        response = _post(client, text="hi")
        assert response.status_code == 200
        assert seen == {"voice": "default", "speed": 150}
