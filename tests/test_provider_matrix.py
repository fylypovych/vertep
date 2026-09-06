"""Tests for Phase 6 — backend matrix helper (``provider_matrix``).

Verifies that the documented Provider Layer exposes a stable, env-driven
description of the active backends across every provider slot, used by the
Web UI (Settings → Engines) and ``/api/status``. No network is touched.
"""

import os

from adapters.providers import provider_matrix

_RELEVANT_ENV = [
    "VERTEP_LLM_PROVIDER",
    "OPENAI_API_KEY",
    "LLM_API_KEY",
    "TTS_PROVIDER",
    "VERTEP_COMPUTE_PROVIDER",
    "COMFYUI_DISTRIBUTED_URL",
    "VERTEP_VIDEO_ENGINE",
    "MONEY_PRINTER_URL",
    "SHORTGPT_URL",
]


def _clean(monkeypatch):
    for key in _RELEVANT_ENV:
        monkeypatch.delenv(key, raising=False)


def test_provider_matrix_defaults(monkeypatch):
    _clean(monkeypatch)
    matrix = provider_matrix()

    assert set(
        ["llm", "tts", "compute", "image", "video", "assembly",
         "video_engine", "publisher"]
    ) <= set(matrix.keys())

    assert matrix["llm"]["backend"] == "ollama"
    assert matrix["llm"]["configured"] is True
    assert matrix["tts"]["backend"] == "none"
    assert matrix["compute"]["backend"] == "vertep-worker"
    assert matrix["image"]["backend"] == "vertep-worker"
    assert matrix["video"]["backend"] == "vertep-worker"
    assert matrix["assembly"]["backend"] == "ffmpeg"
    assert matrix["video_engine"]["backend"] == "native"
    assert "publisher" in matrix


def test_provider_matrix_llm_openai_requires_key(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("VERTEP_LLM_PROVIDER", "openai")
    matrix = provider_matrix()
    assert matrix["llm"]["backend"] == "openai"
    assert matrix["llm"]["configured"] is False

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert provider_matrix()["llm"]["configured"] is True


def test_provider_matrix_compute_comfyui_fallback(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("VERTEP_COMPUTE_PROVIDER", "comfyui-distributed")
    matrix = provider_matrix()
    # No proxy URL configured → falls back to VertepWorker and reports
    # the cluster as not configured.
    assert matrix["compute"]["backend"] == "vertep-worker"
    assert matrix["compute"]["configured"] is False
    assert matrix["image"]["backend"] == "vertep-worker"
    assert matrix["video"]["backend"] == "vertep-worker"


def test_provider_matrix_compute_comfyui_configured(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("VERTEP_COMPUTE_PROVIDER", "comfyui-distributed")
    monkeypatch.setenv("COMFYUI_DISTRIBUTED_URL", "http://comfy:8188")
    matrix = provider_matrix()
    assert matrix["compute"]["backend"] == "comfyui-distributed"
    assert matrix["compute"]["configured"] is True


def test_provider_matrix_video_engine(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("VERTEP_VIDEO_ENGINE", "money-printer")
    # No render URL → native fallback, not configured.
    matrix = provider_matrix()
    assert matrix["video_engine"]["backend"] == "native"
    assert matrix["video_engine"]["configured"] is False

    monkeypatch.setenv("MONEY_PRINTER_URL", "http://mp:8080")
    assert provider_matrix()["video_engine"]["backend"] == "money-printer"

    monkeypatch.setenv("VERTEP_VIDEO_ENGINE", "shortgpt")
    assert provider_matrix()["video_engine"]["backend"] == "native"


def test_provider_matrix_tts_backend_switch(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setenv("TTS_PROVIDER", "piper")
    matrix = provider_matrix()
    assert matrix["tts"]["backend"] == "piper"
    assert "piper" in matrix["tts"]["options"]


def test_provider_matrix_publisher_platforms(monkeypatch):
    _clean(monkeypatch)
    matrix = provider_matrix()
    assert matrix["publisher"]["backend"] == "vertep-official"
    assert isinstance(matrix["publisher"]["platforms"], dict)
    assert "telegram" in matrix["publisher"]["platforms"]
    configured = matrix["publisher"]["platforms"]["telegram"]["configured"]
    assert isinstance(configured, bool)
