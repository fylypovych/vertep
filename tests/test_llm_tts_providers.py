"""Tests for Phase 2 — real open-source LLM/TTS providers.

Covers the interchangeable LLM backends (Ollama / OpenAI-compatible) and the
open-source TTS engines (Piper / Kokoro), plus registry routing and wiring of
``ScriptAgent`` through the backend-agnostic LLM client.
"""

import json
import struct
from pathlib import Path

import httpx

from adapters.providers import get_providers
from adapters.providers.tts_backends import KokoroProvider, PiperProvider
from adapters.llm_clients import (
    OllamaClient,
    OpenAICompatClient,
    get_llm_client,
)


class _Resp:
    def __init__(self, json_body=None, content=b""):
        self._json = json_body
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self._json


def _make_wav(duration: float = 1.0, rate: int = 16000) -> bytes:
    """Build a minimal valid mono 16-bit PCM WAV."""
    channels = 1
    bits = 16
    data_size = int(rate * duration) * channels * (bits // 8)
    fmt = struct.pack(
        "<HHIIHH", 1, channels, rate, rate * channels * (bits // 8),
        channels * (bits // 8), bits,
    )
    data = struct.pack("<I", data_size) + bytes(data_size)
    size = 4 + (8 + len(fmt)) + (8 + len(data))
    return b"RIFF" + struct.pack("<I", size) + b"WAVE" + b"fmt " + \
        struct.pack("<I", len(fmt)) + fmt + b"data" + data


def test_get_llm_client_defaults_to_ollama(monkeypatch):
    monkeypatch.delenv("VERTEP_LLM_PROVIDER", raising=False)
    assert isinstance(get_llm_client(), OllamaClient)


def test_get_llm_client_switch_to_openai(monkeypatch):
    monkeypatch.setenv("VERTEP_LLM_PROVIDER", "openai")
    assert isinstance(get_llm_client(), OpenAICompatClient)


def test_ollama_client_posts_generate(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _Resp(json_body={"response": "hello"})

    monkeypatch.setattr(httpx, "post", fake_post)
    client = OllamaClient(url="http://llm:11434", model="llama3")
    result = client.complete("знайди", format_json=True)
    assert result == "hello"
    assert captured["url"] == "http://llm:11434/api/generate"
    assert captured["json"]["model"] == "llama3"
    assert captured["json"]["format"] == "json"
    assert captured["json"]["stream"] is False


def test_openai_client_posts_chat_completions(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _Resp(json_body={"choices": [{"message": {"content": "hi"}}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    client = OpenAICompatClient(base_url="http://fleet/v1", api_key="k", model="m")
    result = client.complete("привіт", format_json=True)
    assert result == "hi"
    assert captured["url"] == "http://fleet/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer k"
    assert captured["json"]["model"] == "m"
    assert captured["json"]["response_format"] == {"type": "json_object"}


def test_openai_client_retries_without_response_format(monkeypatch):
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        if json.get("response_format"):
            raise httpx.ConnectError("format unsupported")
        return _Resp(json_body={"choices": [{"message": {"content": "plain"}}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    client = OpenAICompatClient(base_url="http://fleet/v1", api_key="k", model="m")
    assert client.complete("x", format_json=True) == "plain"
    assert len(calls) == 2
    assert "response_format" not in calls[1]

def test_piper_provider_cli_writes_real_wav(tmp_path, monkeypatch):
    def fake_run(command, input=b"", check=False, capture_output=False):
        out = Path(command[command.index("--output_file") + 1])
        out.write_bytes(_make_wav(1.0))

    monkeypatch.setattr(
        "adapters.providers.tts_backends.subprocess.run", fake_run
    )
    provider = PiperProvider(binary="piper", model="uk_voice.onnx")
    output = tmp_path / "out.wav"
    result = provider.synthesize("Привіт", output, 2.0, {"name": "v"})
    assert result["provider"] == "piper"
    assert result["status"] == "READY"
    assert output.stat().st_size > 100
    assert abs(result["duration"] - 1.0) < 0.001


def test_piper_provider_http(tmp_path, monkeypatch):
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return _Resp(content=_make_wav(1.0))

    monkeypatch.setattr(httpx, "get", fake_get)
    provider = PiperProvider(url="http://localhost:5000", voice_id="42")
    output = tmp_path / "voice.wav"
    result = provider.synthesize("текст", output)
    assert result["provider"] == "piper"
    assert captured["url"] == "http://localhost:5000/synthesize"
    assert captured["params"]["speaker_id"] == "42"
    assert output.stat().st_size > 100


def test_piper_provider_configured_http_without_binary():
    assert PiperProvider(url="http://localhost:5000").configured() is True


def test_kokoro_provider_posts_speech(tmp_path, monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _Resp(content=_make_wav(1.0))

    monkeypatch.setattr(httpx, "post", fake_post)
    provider = KokoroProvider(base_url="http://kk/v1", api_key="sk", voice="af_heart")
    output = tmp_path / "kokoro.wav"
    result = provider.synthesize("вітаю", output, voice={"voice": "af_heart"})
    assert result["provider"] == "kokoro"
    assert result["status"] == "READY"
    assert captured["url"] == "http://kk/v1/audio/speech"
    assert captured["headers"]["Authorization"] == "Bearer sk"
    assert captured["json"]["model"] == "kokoro"
    assert captured["json"]["voice"] == "af_heart"
    assert output.stat().st_size > 100


def test_kokoro_provider_configured_requires_key():
    assert KokoroProvider(base_url="http://kk").configured() is False
    assert KokoroProvider(base_url="http://kk", api_key="x").configured() is True


def test_registry_routes_piper_and_kokoro():
    reg = get_providers()
    assert isinstance(reg.tts("piper"), PiperProvider)
    assert isinstance(reg.tts("kokoro"), KokoroProvider)
    assert reg.tts("mock").provider == "mock"
    assert reg.tts("none").provider == "none"


def test_script_agent_uses_backend_agnostic_client(monkeypatch):
    class FakeLLM:
        def __init__(self):
            self.calls = []

        def complete(self, prompt, format_json=False):
            self.calls.append(format_json)
            if "Згенеруй структуру" in prompt:
                return json.dumps({
                    "title": "Тема",
                    "description": "опис",
                    "hashtags": ["#v"],
                    "voiceover": "закадровий",
                })
            return json.dumps({
                "prompt": "картинка",
                "video_prompt": "анімація",
                "voiceover": "сцена",
                "duration": 4,
            })

    monkeypatch.setenv("DEMO_MODE", "false")
    fake = FakeLLM()
    monkeypatch.setattr("core.script_agent.get_llm_client", lambda: fake)

    from core.script_agent import ScriptAgent

    script = ScriptAgent().generate_script("нова тема")
    assert script["scenes"], "expected at least one normalized scene"
    assert len(script["scenes"]) == 1
    assert fake.calls, "LLM client must be invoked"
    assert all(call is True for call in fake.calls), "format_json must be on"
    assert script["title"] == "Тема"
