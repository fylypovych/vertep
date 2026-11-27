"""Tests for #540 (i.0.0.5.40): hybrid Piper uk_UA + espeak-ng TTS runtime."""

from __future__ import annotations

import base64
import io
import json
import wave
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from adapters.providers.tts_backends import PiperProvider
from services import tts_service

ROOT = Path(__file__).resolve().parents[1]


def _make_wav_bytes(frames: int = 1600, rate: int = 22050) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x01\x00" * frames)
    return buf.getvalue()


def _install_fake_piper_models(tmp_path: Path) -> Path:
    models_dir = tmp_path / "piper-models"
    models_dir.mkdir()
    for voice_id, lang_code, name in [
        ("uk_UA-ukrainian_tts-medium", "uk_UA", "Ukrainian"),
        ("uk_UA-lada-x_low", "uk_UA", "Ukrainian"),
    ]:
        (models_dir / f"{voice_id}.onnx").write_bytes(b"fake-onnx")
        (models_dir / f"{voice_id}.onnx.json").write_text(
            json.dumps({"language": {"code": lang_code, "family": "uk", "name_english": name}}),
            encoding="utf-8",
        )
    return models_dir


def test_tts_dockerfile_bundles_piper_uk_models_with_sha256_and_keeps_espeak():
    dockerfile = (ROOT / "docker" / "tts" / "Dockerfile").read_text(encoding="utf-8")
    assert "piper-tts==" in dockerfile
    assert "espeak-ng" in dockerfile
    assert "uk_UA-ukrainian_tts-medium.onnx" in dockerfile
    assert "uk_UA-ukrainian_tts-medium.onnx.json" in dockerfile
    assert "uk_UA-lada-x_low.onnx" in dockerfile
    assert "uk_UA-lada-x_low.onnx.json" in dockerfile
    assert "sha256sum -c -" in dockerfile
    assert "7920419ac5f6fd8b6450520f24b52ed5a319cb53dd018fbcd71c9e079cbac84f" in dockerfile
    assert "f8d015d3fc19ea6bd8ac3ca1fd0c0fbac5054c609599ee73799235fd2cf5c786" in dockerfile


def test_hybrid_tts_routes_uk_to_piper_and_other_languages_to_espeak(monkeypatch, tmp_path):
    models_dir = _install_fake_piper_models(tmp_path)
    piper_wav = _make_wav_bytes(frames=2205, rate=22050)
    espeak_wav = _make_wav_bytes(frames=1600, rate=16000)
    calls: list[list[str]] = []

    def fake_which(name: str):
        if name == "piper":
            return "/usr/local/bin/piper"
        if name == "espeak-ng":
            return "/usr/bin/espeak-ng"
        return None

    def fake_run(command, **kwargs):
        calls.append(list(command))
        if "--voices" in command:
            return SimpleNamespace(
                stdout=(
                    "Pty Language Age/Gender VoiceName File\n"
                    "5 uk --/M Ukrainian zle/uk\n"
                    "5 en-us --/M English_(America) gmw/en-US\n"
                )
            )
        if command[0] == "/usr/local/bin/piper":
            out = Path(command[command.index("--output_file") + 1])
            out.write_bytes(piper_wav)
            return SimpleNamespace(stdout=b"", stderr=b"")
        return SimpleNamespace(stdout=espeak_wav, stderr=b"")

    monkeypatch.setenv("PIPER_MODELS_DIR", str(models_dir))
    monkeypatch.setenv("PIPER_DEFAULT_UK_VOICE", "uk_UA-ukrainian_tts-medium")
    monkeypatch.setattr(tts_service, "VOICES", [])
    monkeypatch.setattr(tts_service, "_VOICE_DATA_DIR", str(tmp_path / "no-espeak-dir"))
    monkeypatch.setattr(tts_service.shutil, "which", fake_which)
    monkeypatch.setattr(tts_service.subprocess, "run", fake_run)

    client = TestClient(tts_service.app)

    # /health advertises both engines with piper primary
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["engine"] == "piper"
    assert health.json()["engines"] == ["piper", "espeak-ng"]

    # /voices advertises real Piper + espeak voices, providers, models, languages
    catalog = client.get("/voices").json()
    assert catalog["default_voice"] == "uk_UA-ukrainian_tts-medium"
    assert catalog["providers"] == ["piper", "espeak-ng"]
    assert "uk_UA-ukrainian_tts-medium" in catalog["models"]
    assert "uk_UA-lada-x_low" in catalog["models"]
    assert "espeak-ng" in catalog["models"]
    assert {"uk", "en"} <= set(catalog["languages"])
    voice_ids = {v["id"] for v in catalog["voices"]}
    assert {"uk_UA-ukrainian_tts-medium", "uk_UA-lada-x_low", "uk", "en-us"} <= voice_ids

    # Default Ukrainian synthesis routes to Piper uk_UA-ukrainian_tts-medium
    resp_uk = client.post("/synthesize", json={"text": "Привіт, Україно!", "voice": "uk", "speed": 150})
    assert resp_uk.status_code == 200
    body_uk = resp_uk.json()
    assert body_uk["engine"] == "piper"
    assert body_uk["voice"] == "uk_UA-ukrainian_tts-medium"
    assert base64.b64decode(body_uk["audio_base64"]).startswith(b"RIFF")

    # Explicit second Piper voice uk_UA-lada-x_low
    resp_lada = client.post(
        "/synthesize",
        json={"text": "Добрий день", "voice": "uk_UA-lada-x_low", "provider": "piper", "language": "uk", "speed": 120},
    )
    assert resp_lada.status_code == 200
    assert resp_lada.json()["engine"] == "piper"
    assert resp_lada.json()["voice"] == "uk_UA-lada-x_low"
    piper_call = [c for c in calls if c[0] == "/usr/local/bin/piper"][-1]
    assert "1.250" in piper_call  # 150 / 120 = 1.250

    # Other language falls back to espeak-ng
    resp_en = client.post(
        "/synthesize",
        json={"text": "Hello world", "voice": "en-us", "language": "en", "speed": 150},
    )
    assert resp_en.status_code == 200
    assert resp_en.json()["engine"] == "espeak-ng"
    assert resp_en.json()["voice"] == "en-us"

    # Explicit espeak-ng on uk still works when requested explicitly
    resp_espeak_uk = client.post(
        "/synthesize",
        json={"text": "Тест", "voice": "uk", "language": "uk", "provider": "espeak-ng", "model": "espeak-ng"},
    )
    assert resp_espeak_uk.status_code == 200
    assert resp_espeak_uk.json()["engine"] == "espeak-ng"

    # Fail-closed rejections
    assert client.post("/synthesize", json={"text": "x", "voice": "uk_UA-ukrainian_tts-medium", "language": "en"}).status_code == 422
    assert client.post("/synthesize", json={"text": "x", "voice": "unknown_piper", "provider": "piper"}).status_code == 422
    assert client.post("/synthesize", json={"text": "x", "voice": "uk", "provider": "mock"}).status_code == 422
    assert client.post("/synthesize", json={"text": "x", "voice": "uk", "provider": "piper", "model": "other"}).status_code == 422


def test_piper_provider_supports_vertep_tts_synthesize_json_endpoint(monkeypatch, tmp_path):
    wav = _make_wav_bytes(frames=1600, rate=16000)
    captured = {}

    class JsonResp:
        headers = {"content-type": "application/json"}

        def raise_for_status(self):
            return None

        def json(self):
            return {"audio_base64": base64.b64encode(wav).decode("ascii"), "mime_type": "audio/wav", "engine": "piper"}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return JsonResp()

    monkeypatch.setattr(httpx, "post", fake_post)
    out = tmp_path / "speech.wav"
    provider = PiperProvider(url="http://tts:8090/synthesize", voice_id="uk_UA-ukrainian_tts-medium")
    result = provider.synthesize("Привіт", out)
    assert captured["url"] == "http://tts:8090/synthesize"
    assert captured["json"] == {"text": "Привіт", "provider": "piper", "voice": "uk_UA-ukrainian_tts-medium"}
    assert result["provider"] == "piper"
    assert out.read_bytes() == wav
