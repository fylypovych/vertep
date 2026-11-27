"""Локальний GPU HTTP runtime для lip-sync анімації аватара на базі MuseTalk (MIT, #551).

Працює без некомерційних залежностей і ваг InsightFace (використовуються лише
комерційно сумісні компоненти MuseTalk: SD-VAE, Whisper, DWPose/MediaPipe).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import importlib.util
import os
import subprocess
import tempfile
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from adapters import media_integrity
from adapters.providers.lipsync_backends import (
    LIPSYNC_CONTRACT_FORMAT,
    PORTRAIT_SUFFIXES,
    validate_wav_bytes,
)

app = FastAPI(title="Vertep MuseTalk Lip-Sync", version="1")

MAX_PORTRAIT_BYTES = 50 * 1024 * 1024  # 50 MiB
MAX_AUDIO_BYTES = 50 * 1024 * 1024     # 50 MiB


def _musetalk_entry() -> Path | None:
    custom = os.getenv("MUSETALK_INFERENCE_SCRIPT", "").strip()
    if custom:
        candidate = Path(custom)
        return candidate if candidate.is_file() else None
    root = Path(os.getenv("MUSETALK_ROOT", "/opt/musetalk"))
    entry = root / "scripts" / "inference.py"
    return entry if entry.is_file() else None


def _insightface_absent() -> bool:
    return importlib.util.find_spec("insightface") is None


class LipSyncRequest(BaseModel):
    portrait_base64: str = Field(min_length=1)
    portrait_filename: str = Field(default="avatar.png", max_length=128)
    audio_base64: str = Field(min_length=1)
    audio_filename: str = Field(default="speech.wav", max_length=128)
    fps: int = Field(default=25, ge=12, le=60)
    bbox_shift: int = Field(default=0, ge=-64, le=64)
    character_id: str | None = Field(default=None, max_length=64)


@app.get("/health")
def health() -> dict:
    if not _insightface_absent():
        raise HTTPException(503, "Виявлено заборонену некомерційну залежність insightface")
    if _musetalk_entry() is None:
        raise HTTPException(503, "MuseTalk runtime недоступний")
    return {
        "status": "HEALTHY",
        "engine": "musetalk",
        "license": "MIT",
        "insightface_excluded": True,
    }


def _decode_b64(encoded: str, label: str, max_bytes: int) -> bytes:
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise HTTPException(422, f"Некоректний base64 для {label}") from error
    if not raw:
        raise HTTPException(422, f"Порожній файл {label}")
    if len(raw) > max_bytes:
        raise HTTPException(413, f"Файл {label} перевищує допустимий розмір")
    return raw


@app.post("/lipsync")
def lipsync(request: LipSyncRequest) -> dict:
    portrait_suffix = Path(request.portrait_filename or "avatar.png").suffix.lower()
    if portrait_suffix not in PORTRAIT_SUFFIXES:
        raise HTTPException(422, f"Непідтримуваний формат портрета: {portrait_suffix or 'unknown'}")
    audio_suffix = Path(request.audio_filename or "speech.wav").suffix.lower()
    if audio_suffix != ".wav":
        raise HTTPException(422, f"Очікується WAV-аудіо, отримано: {audio_suffix or 'unknown'}")

    portrait_bytes = _decode_b64(request.portrait_base64, "портрета", MAX_PORTRAIT_BYTES)
    audio_bytes = _decode_b64(request.audio_base64, "аудіо", MAX_AUDIO_BYTES)

    try:
        media_integrity.verify_media_bytes(portrait_bytes, portrait_suffix)
    except media_integrity.MediaIntegrityError as error:
        raise HTTPException(422, f"Некоректні медіадані портрета: {error}") from error

    try:
        validate_wav_bytes(audio_bytes)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error

    if not _insightface_absent():
        raise HTTPException(503, "Виявлено заборонену некомерційну залежність insightface")
    entry = _musetalk_entry()
    if entry is None:
        raise HTTPException(503, "MuseTalk runtime недоступний")

    portrait_sha256 = hashlib.sha256(portrait_bytes).hexdigest()
    audio_sha256 = hashlib.sha256(audio_bytes).hexdigest()

    with tempfile.TemporaryDirectory(prefix="vertep-musetalk-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        portrait_path = tmp_root / f"portrait{portrait_suffix}"
        audio_path = tmp_root / "speech.wav"
        output_dir = tmp_root / "output"
        output_dir.mkdir(parents=True, exist_ok=True)

        portrait_path.write_bytes(portrait_bytes)
        audio_path.write_bytes(audio_bytes)

        inference_cfg = {
            "task_0": {
                "video_path": str(portrait_path),
                "audio_path": str(audio_path),
                "bbox_shift": int(request.bbox_shift),
            }
        }
        cfg_path = tmp_root / "inference.yaml"
        cfg_path.write_text(yaml.safe_dump(inference_cfg, sort_keys=True), encoding="utf-8")

        python_bin = os.getenv("MUSETALK_PYTHON", "python3")
        cmd = [
            python_bin,
            str(entry),
            "--inference_config",
            str(cfg_path),
            "--result_dir",
            str(output_dir),
            "--fps",
            str(request.fps),
            "--bbox_shift",
            str(request.bbox_shift),
        ]
        try:
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=int(os.getenv("MUSETALK_TIMEOUT_SECONDS", "600")),
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
            raise HTTPException(502, "MuseTalk не зміг завершити lip-sync анімацію") from error

        videos = sorted(output_dir.rglob("*.mp4"))
        if not videos:
            raise HTTPException(502, "MuseTalk завершився без вихідного .mp4 відео")
        video_bytes = videos[-1].read_bytes()
        try:
            mime_type = media_integrity.verify_media_bytes(video_bytes, ".mp4")
        except media_integrity.MediaIntegrityError as error:
            raise HTTPException(502, f"MuseTalk повернув пошкоджене відео: {error}") from error

    video_sha256 = hashlib.sha256(video_bytes).hexdigest()
    prefix = (request.character_id or "avatar").strip() or "avatar"
    return {
        "format": LIPSYNC_CONTRACT_FORMAT,
        "engine": "musetalk",
        "license": "MIT",
        "character_id": request.character_id,
        "fps": int(request.fps),
        "bbox_shift": int(request.bbox_shift),
        "mime_type": mime_type,
        "filename": f"{prefix}-lipsync-{video_sha256[:12]}.mp4",
        "sha256": video_sha256,
        "size": len(video_bytes),
        "audio_sha256": audio_sha256,
        "portrait_sha256": portrait_sha256,
        "video_base64": base64.b64encode(video_bytes).decode("ascii"),
    }
