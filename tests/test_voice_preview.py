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

        monkeypatch.setattr(models_api, "voice_catalog", _boom)
        monkeypatch.setattr(models_api, "synthesize_voice", _boom)
        assert _post(client, text="hi").status_code == 409


class TestPreviewRuntimeFailures:
    def test_unreachable_runtime_is_502(self, client, monkeypatch):
        monkeypatch.setenv("TTS_PROVIDER", "mock")

        def _raise(*_args, **_kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(models_api, "voice_catalog", _raise)
        response = _post(client, text="hi")
        assert response.status_code == 502
        assert "unreachable" in response.json()["detail"]

    def test_unreadable_catalog_is_502(self, client, monkeypatch):
        monkeypatch.setenv("TTS_PROVIDER", "mock")

        def _raise(*_args, **_kwargs):
            raise ValueError("malformed payload")

        monkeypatch.setattr(models_api, "voice_catalog", _raise)
        response = _post(client, text="hi")
        assert response.status_code == 502
        assert "unreadable" in response.json()["detail"]

    def test_synthesis_failure_is_502(self, client, monkeypatch):
        monkeypatch.setenv("TTS_PROVIDER", "mock")
        monkeypatch.setattr(models_api, "voice_catalog", lambda: {"voices": []})

        def _raise(*_args, **_kwargs):
            raise httpx.ConnectError("tts down")

        monkeypatch.setattr(models_api, "synthesize_voice", _raise)
        assert _post(client, text="hi").status_code == 502

    def test_empty_audio_is_502(self, client, monkeypatch):
        monkeypatch.setenv("TTS_PROVIDER", "mock")
        monkeypatch.setattr(models_api, "voice_catalog", lambda: {"voices": []})
        monkeypatch.setattr(models_api, "synthesize_voice", lambda *a, **k: b"")
        assert _post(client, text="hi").status_code == 502


class TestPreviewVoices:
    def test_unknown_voice_is_422(self, client, monkeypatch):
        monkeypatch.setenv("TTS_PROVIDER", "mock")
        monkeypatch.setattr(models_api, "voice_catalog", lambda: {"voices": [{"name": "uk"}]})
        monkeypatch.setattr(
            models_api, "synthesize_voice",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no synthesis")))
        response = _post(client, text="hi", voice="nope")
        assert response.status_code == 422
        assert "Unknown voice" in response.json()["detail"]

    def test_known_voice_reaches_synthesis(self, client, monkeypatch):
        monkeypatch.setenv("TTS_PROVIDER", "mock")
        monkeypatch.setattr(models_api, "voice_catalog", lambda: {"voices": [{"name": "uk"}]})
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
        monkeypatch.setattr(models_api, "voice_catalog", lambda: {"voices": []})
        seen = {}

        def _synth(text, voice, speed):
            seen.update(voice=voice, speed=speed)
            return b"RIFF\x04\x00\x00\x00WAVE"

        monkeypatch.setattr(models_api, "synthesize_voice", _synth)
        response = _post(client, text="hi")
        assert response.status_code == 200
        assert seen == {"voice": "default", "speed": 150}


class TestIssue455Contract:
    """The runtime answers {id, name} voices; CORE keeps the id for synthesis."""

    RUNTIME = {"voices": [{"id": "uk", "name": "Ukrainian", "engine": "espeak-ng"},
                          {"id": "en-us", "name": "English (America)", "engine": "espeak-ng"}],
               "default_voice": "default", "speed_range": {"min": 80, "max": 450}}

    def test_catalog_is_normalized_to_id_and_name(self, client, monkeypatch):
        monkeypatch.setattr(models_api, "voice_catalog", lambda: self.RUNTIME)
        body = client.get("/api/models/voices").json()
        assert body["voices"] == [{"id": "uk", "name": "Ukrainian"}, {"id": "en-us", "name": "English (America)"}]
        assert body["default_voice"] == "default" and body["speed_range"] == {"min": 80, "max": 450}
        assert body["speed_unit"] == "words_per_minute"

    def test_unreachable_catalog_has_code_without_internal_detail(self, client, monkeypatch):
        def _raise():
            raise httpx.ConnectError("connect to http://tts:8090 refused")
        monkeypatch.setattr(models_api, "voice_catalog", _raise)
        response = client.get("/api/models/voices")
        assert response.status_code == 502
        assert response.json() == {"detail": "TTS runtime is unreachable", "code": "tts_runtime_is_unreachable"}

    def test_preview_sends_the_id_and_accepts_the_engine_default(self, client, monkeypatch):
        monkeypatch.setenv("TTS_PROVIDER", "mock")
        monkeypatch.setattr(models_api, "voice_catalog", lambda: self.RUNTIME)
        seen = []
        monkeypatch.setattr(models_api, "synthesize_voice", lambda text, voice, speed: seen.append(voice) or _wav())
        assert _post(client, text="hi", voice="uk").status_code == 200
        assert _post(client, text="hi").status_code == 200  # default is not a catalog entry
        assert seen == ["uk", "default"]
        unknown = _post(client, text="hi", voice="[object Object]")
        assert unknown.status_code == 422 and unknown.json()["code"] == "unknown_voice"

    @pytest.mark.parametrize("speed", [79, 451])
    def test_speed_follows_the_runtime_range(self, client, monkeypatch, speed):
        monkeypatch.setenv("TTS_PROVIDER", "mock")
        monkeypatch.setattr(models_api, "voice_catalog", lambda: self.RUNTIME)
        monkeypatch.setattr(models_api, "synthesize_voice",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no synthesis")))
        response = _post(client, text="hi", speed=speed)
        assert response.status_code == 422
        assert response.json()["code"] == "speed_is_outside_the_range_of_the_tts_runtime"
