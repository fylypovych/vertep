"""Local speech-to-text provider backend using faster-whisper HTTP runtime (#544)."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Callable

import httpx

from .base import STTProvider


class FasterWhisperProvider(STTProvider):
    """Local ``faster-whisper`` (MIT) STT provider communicating over HTTP.

    Configuration:
    * ``STT_URL`` (e.g. ``http://stt:8099``)
    * ``STT_MODEL`` (optional default model override, e.g. ``small``)
    """

    def __init__(
        self,
        url: str | None = None,
        model: str | None = None,
        post: Callable | None = None,
    ) -> None:
        self._url = (url if url is not None else os.getenv("STT_URL", "")).rstrip("/")
        self._model = (model if model is not None else os.getenv("STT_MODEL", "")).strip() or None
        self._post = post

    @property
    def provider(self) -> str:
        return "faster-whisper"

    def configured(self) -> bool:
        return bool(self._url)

    def transcribe_bytes(
        self,
        audio_bytes: bytes,
        *,
        filename: str = "audio.wav",
        language: str | None = None,
        model: str | None = None,
        vad_filter: bool = True,
    ) -> dict:
        if not self._url:
            raise RuntimeError("STT runtime URL (STT_URL) is not configured")
        if not audio_bytes:
            raise ValueError("Audio payload is empty")
        expected_sha256 = hashlib.sha256(audio_bytes).hexdigest()
        payload: dict = {
            "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
            "filename": filename or "audio.wav",
            "vad_filter": bool(vad_filter),
        }
        if language:
            payload["language"] = str(language).strip()
        selected_model = (model or self._model or "").strip()
        if selected_model:
            payload["model"] = selected_model

        endpoint = f"{self._url}/transcribe"
        if self._post is not None:
            response = self._post(endpoint, json=payload, timeout=300)
        else:
            response = httpx.post(endpoint, json=payload, timeout=300)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            raise RuntimeError("STT runtime returned an invalid transcription payload")
        returned_sha = body.get("audio_sha256")
        if returned_sha and returned_sha != expected_sha256:
            raise RuntimeError("STT runtime audio_sha256 does not match submitted audio")
        return {
            "format": body.get("format") or "stt_contract/v1",
            "provider": self.provider,
            "engine": body.get("engine") or "faster-whisper",
            "model": body.get("model") or selected_model or None,
            "language": body.get("language"),
            "language_probability": body.get("language_probability"),
            "duration": body.get("duration"),
            "text": body["text"],
            "segments": body.get("segments") if isinstance(body.get("segments"), list) else [],
            "audio_sha256": expected_sha256,
        }

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        model: str | None = None,
    ) -> dict:
        path = Path(audio_path)
        if not path.is_file():
            raise FileNotFoundError(f"Audio file not found: {path}")
        return self.transcribe_bytes(
            path.read_bytes(),
            filename=path.name,
            language=language,
            model=model,
        )
