"""Локальний STT runtime на базі faster-whisper (MIT, #544)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Vertep STT", version="1")

LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$")
ALLOWED_EXTENSIONS = frozenset({".wav", ".mp3", ".ogg", ".flac", ".m4a", ".mp4", ".webm", ".opus"})
MAX_AUDIO_BYTES = 50 * 1024 * 1024  # 50 MiB

_model_lock = threading.Lock()
_model_cache: dict[tuple[str, str, str, str], Any] = {}


def _default_model() -> str:
    return os.getenv("STT_MODEL", "small").strip() or "small"


def _default_device() -> str:
    return os.getenv("STT_DEVICE", "cpu").strip() or "cpu"


def _default_compute_type() -> str:
    return os.getenv("STT_COMPUTE_TYPE", "int8").strip() or "int8"


def _models_dir() -> str:
    return os.getenv("STT_MODELS_DIR", "/data/models/stt").strip() or "/data/models/stt"


def _whisper_model_class():
    try:
        from faster_whisper import WhisperModel
        return WhisperModel
    except ImportError:
        return None


def _load_model(model_name: str):
    key = (model_name, _default_device(), _default_compute_type(), _models_dir())
    with _model_lock:
        cached = _model_cache.get(key)
        if cached is not None:
            return cached
        cls = _whisper_model_class()
        if cls is None:
            raise HTTPException(503, "faster-whisper runtime недоступний")
        try:
            instance = cls(
                model_name,
                device=key[1],
                compute_type=key[2],
                download_root=key[3],
            )
        except Exception as error:  # noqa: BLE001
            raise HTTPException(503, f"Не вдалося завантажити модель faster-whisper: {error}") from error
        _model_cache[key] = instance
        return instance


class TranscribeRequest(BaseModel):
    audio_base64: str = Field(min_length=1)
    filename: str = Field(default="audio.wav", max_length=128)
    language: str | None = Field(default=None, max_length=16)
    model: str | None = Field(default=None, max_length=64)
    vad_filter: bool = Field(default=True)


@app.get("/health")
def health() -> dict:
    if _whisper_model_class() is None:
        raise HTTPException(503, "faster-whisper недоступний")
    return {
        "status": "HEALTHY",
        "engine": "faster-whisper",
        "model": _default_model(),
        "device": _default_device(),
        "compute_type": _default_compute_type(),
    }


def _safe_extension(filename: str) -> str:
    suffix = Path(filename or "audio.wav").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(422, f"Непідтримуваний формат аудіо: {suffix or 'unknown'}")
    return suffix


def _normalize_language(language: str | None) -> str | None:
    if language is None:
        return None
    cleaned = language.strip().lower()
    if not cleaned or cleaned == "auto":
        return None
    if not LANGUAGE_RE.fullmatch(cleaned):
        raise HTTPException(422, f"Некоректний код мови: {language!r}")
    return cleaned.split("-")[0]


@app.post("/transcribe")
def transcribe(request: TranscribeRequest) -> dict:
    suffix = _safe_extension(request.filename)
    language = _normalize_language(request.language)
    model_name = (request.model or _default_model()).strip()
    if not MODEL_RE.fullmatch(model_name) or ".." in model_name:
        raise HTTPException(422, f"Некоректна назва моделі STT: {model_name!r}")

    try:
        audio_bytes = base64.b64decode(request.audio_base64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise HTTPException(422, "Некоректний base64-вміст аудіо") from error

    if not audio_bytes:
        raise HTTPException(422, "Порожній аудіофайл")
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "Аудіофайл перевищує допустимий розмір")

    audio_sha256 = hashlib.sha256(audio_bytes).hexdigest()
    whisper = _load_model(model_name)

    try:
        with tempfile.TemporaryDirectory(prefix="vertep-stt-") as tmp_dir:
            audio_path = Path(tmp_dir) / f"input{suffix}"
            audio_path.write_bytes(audio_bytes)
            raw_segments, info = whisper.transcribe(
                str(audio_path),
                language=language,
                vad_filter=bool(request.vad_filter),
            )
            segments = []
            parts = []
            for idx, seg in enumerate(raw_segments):
                seg_text = str(getattr(seg, "text", "") or "").strip()
                segments.append({
                    "id": idx,
                    "start": round(float(getattr(seg, "start", 0.0)), 3),
                    "end": round(float(getattr(seg, "end", 0.0)), 3),
                    "text": seg_text,
                })
                if seg_text:
                    parts.append(seg_text)
    except HTTPException:
        raise
    except Exception as error:  # noqa: BLE001
        raise HTTPException(502, "STT engine не зміг виконати транскрипцію") from error

    detected_lang = getattr(info, "language", None) or language
    lang_prob = getattr(info, "language_probability", None)
    duration = getattr(info, "duration", None)
    return {
        "format": "stt_contract/v1",
        "engine": "faster-whisper",
        "model": model_name,
        "language": str(detected_lang) if detected_lang else None,
        "language_probability": round(float(lang_prob), 4) if lang_prob is not None else None,
        "duration": round(float(duration), 3) if duration is not None else None,
        "text": " ".join(parts).strip(),
        "segments": segments,
        "audio_sha256": audio_sha256,
    }
