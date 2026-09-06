"""Real open-source TTS backends behind the TTS provider layer.

Phase 2 of the open-source audit replaces the "not implemented" live TTS path
with two engines:

* ``PiperProvider``  — Piper (MIT). Uses the ``piper`` CLI or its HTTP server.
* ``KokoroProvider`` — Kokoro (Apache-2.0). Uses an OpenAI-compatible
  ``/v1/audio/speech`` HTTP endpoint (the standard way to expose Kokoro).

Both implement the ``TTSProvider`` interface so they are directly swappable in
the provider registry without touching pipeline/worker code.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
from pathlib import Path

import httpx

from .base import TTSProvider


def _write_manifest(provider: str, output: Path, text: str, duration: float,
                    voice: dict | None, extra: dict | None = None) -> dict:
    manifest = {
        "status": "READY",
        "provider": provider,
        "text": text,
        "duration": duration,
        "voice": voice or {},
        "path": str(output),
    }
    if extra:
        manifest.update(extra)
    output.with_suffix(".json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def _wav_duration(path: Path) -> float:
    """Approximate duration (seconds) from a PCM WAV header, if readable."""
    try:
        data = path.read_bytes()
        if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            return 0.0
        rate = sample_width = None
        pos = 12
        while pos + 8 <= len(data):
            chunk_id = data[pos:pos + 4]
            size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
            if chunk_id == b"fmt ":
                sample_width = struct.unpack("<H", data[pos + 22:pos + 24])[0]
                rate = struct.unpack("<I", data[pos + 12:pos + 16])[0]
            elif chunk_id == b"data" and rate and sample_width:
                sample_frames = struct.unpack("<I", data[pos + 4:pos + 8])[0]
                return sample_frames / max(1, rate) / max(1, sample_width // 8)
            pos += 8 + size
    except (OSError, struct.error):
        return 0.0
    return 0.0
class PiperProvider(TTSProvider):
    """Piper text-to-speech (MIT). CLI or HTTP server mode.

    Configuration:

    * HTTP: ``PIPER_URL`` (e.g. ``http://localhost:5000``) — ``/synthesize``.
    * CLI:  ``PIPER_BIN`` (default ``piper``) + ``PIPER_MODEL`` (.onnx).
    """

    def __init__(
        self,
        url: str | None = None,
        binary: str | None = None,
        model: str | None = None,
        voice_id: str | None = None,
    ) -> None:
        self._url = (url or os.getenv("PIPER_URL", "")).rstrip("/")
        self._binary = binary or os.getenv("PIPER_BIN", "piper")
        self._model = model or os.getenv("PIPER_MODEL", "")
        self._voice_id = voice_id

    @property
    def provider(self) -> str:
        return "piper"

    def configured(self) -> bool:
        if self._url:
            return True
        return bool(self._model) and shutil.which(self._binary) is not None

    def _synthesize_http(self, text: str, output: Path) -> float:
        url = f"{self._url}/synthesize"
        params = {"text": text}
        if self._voice_id:
            params["speaker_id"] = self._voice_id
        response = httpx.get(url, params=params, timeout=120)
        response.raise_for_status()
        output.write_bytes(response.content)
        return _wav_duration(output)

    def _synthesize_cli(self, text: str, output: Path) -> float:
        command = [self._binary, "--model", self._model, "--output_file", str(output)]
        subprocess.run(
            command, input=text.encode("utf-8"), check=True, capture_output=True
        )
        return _wav_duration(output)

    def synthesize(
        self,
        text: str,
        output: Path,
        duration: float = 3.0,
        voice: dict | None = None,
    ) -> dict:
        output.parent.mkdir(parents=True, exist_ok=True)
        if self._url:
            actual = self._synthesize_http(text, output)
        else:
            actual = self._synthesize_cli(text, output)
        real_duration = actual or max(0.2, duration)
        return _write_manifest("piper", output, text, real_duration, voice,
                               {"engine": "piper"})


class KokoroProvider(TTSProvider):
    """Kokoro text-to-speech (Apache-2.0) via OpenAI-compatible speech endpoint.

    Talks to ``POST /v1/audio/speech`` and expects raw audio bytes back.
    Configuration:

    * ``KOKORO_URL``     (defaults to ``OPENAI_BASE_URL`` if set)
    * ``KOKORO_API_KEY`` (Bearer token)
    * ``KOKORO_MODEL``   (default ``kokoro``)
    * ``KOKORO_VOICE``   (default speaker, e.g. ``af_heart``)
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        voice: str | None = None,
    ) -> None:
        self._base_url = (
            base_url
            or os.getenv("KOKORO_URL")
            or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        ).rstrip("/")
        self._api_key = api_key or os.getenv("KOKORO_API_KEY", "")
        self._model = model or os.getenv("KOKORO_MODEL", "kokoro")
        self._voice = voice or os.getenv("KOKORO_VOICE", "")

    @property
    def provider(self) -> str:
        return "kokoro"

    def configured(self) -> bool:
        return bool(self._api_key) and bool(self._base_url)

    def synthesize(
        self,
        text: str,
        output: Path,
        duration: float = 3.0,
        voice: dict | None = None,
    ) -> dict:
        output.parent.mkdir(parents=True, exist_ok=True)
        headers = {"Authorization": f"Bearer {self._api_key}"}
        payload: dict = {
            "model": self._model,
            "input": text,
            "voice": self._voice or (voice or {}).get("voice", ""),
        }
        response = httpx.post(
            f"{self._base_url}/audio/speech", headers=headers, json=payload,
            timeout=120,
        )
        response.raise_for_status()
        output.write_bytes(response.content)
        actual = _wav_duration(output)
        real_duration = actual or max(0.2, duration)
        return _write_manifest("kokoro", output, text, real_duration, voice,
                               {"engine": "kokoro"})