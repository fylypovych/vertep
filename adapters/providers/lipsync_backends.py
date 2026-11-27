"""Local lip-sync avatar animation provider backend using MuseTalk (MIT, #551)."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Callable

import httpx

from adapters import media_integrity
from .base import LipSyncProvider

LIPSYNC_CONTRACT_FORMAT = "lipsync_contract/v1"
DEFAULT_LIPSYNC_MIN_VRAM_MB = 8192
PORTRAIT_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".webm"})


def validate_wav_bytes(data: bytes) -> None:
    """Verify minimal RIFF/WAVE PCM header structure."""
    if not data or len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("Audio payload must be a valid WAV (RIFF/WAVE) file")


def validate_portrait_bytes(data: bytes, filename: str = "avatar.png") -> str:
    """Structurally verify an avatar portrait image or driving video."""
    suffix = Path(filename or "avatar.png").suffix.lower()
    if suffix not in PORTRAIT_SUFFIXES:
        raise ValueError(f"Unsupported portrait media suffix: {suffix or 'unknown'}")
    return media_integrity.verify_media_bytes(data, suffix)


class MuseTalkProvider(LipSyncProvider):
    """Local ``MuseTalk`` (MIT) lip-sync provider communicating over HTTP.

    Runs strictly without non-commercial InsightFace weights/dependencies.

    Configuration:
    * ``MUSETALK_URL`` (e.g. ``http://musetalk:8102``)
    * ``MUSETALK_TIMEOUT_SECONDS`` (optional HTTP timeout, default ``600``)
    """

    def __init__(
        self,
        url: str | None = None,
        post: Callable | None = None,
        timeout: float | None = None,
    ) -> None:
        self._url = (url if url is not None else os.getenv("MUSETALK_URL", "")).rstrip("/")
        self._post = post
        self._timeout = (
            float(timeout)
            if timeout is not None
            else float(os.getenv("MUSETALK_TIMEOUT_SECONDS", "600"))
        )

    @property
    def provider(self) -> str:
        return "musetalk"

    def configured(self) -> bool:
        return bool(self._url)

    def animate_bytes(
        self,
        portrait_bytes: bytes,
        audio_bytes: bytes,
        *,
        portrait_filename: str = "avatar.png",
        audio_filename: str = "speech.wav",
        fps: int = 25,
        bbox_shift: int = 0,
        character_id: str | None = None,
    ) -> tuple[bytes, dict]:
        if not self._url:
            raise RuntimeError("MuseTalk runtime URL (MUSETALK_URL) is not configured")
        if fps < 12 or fps > 60:
            raise ValueError("fps must be between 12 and 60")
        if bbox_shift < -64 or bbox_shift > 64:
            raise ValueError("bbox_shift must be between -64 and 64")

        validate_portrait_bytes(portrait_bytes, portrait_filename)
        validate_wav_bytes(audio_bytes)

        portrait_sha256 = hashlib.sha256(portrait_bytes).hexdigest()
        audio_sha256 = hashlib.sha256(audio_bytes).hexdigest()
        payload: dict = {
            "portrait_base64": base64.b64encode(portrait_bytes).decode("ascii"),
            "portrait_filename": Path(portrait_filename or "avatar.png").name,
            "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
            "audio_filename": Path(audio_filename or "speech.wav").name,
            "fps": int(fps),
            "bbox_shift": int(bbox_shift),
        }
        if character_id:
            payload["character_id"] = str(character_id).strip()

        endpoint = f"{self._url}/lipsync"
        if self._post is not None:
            response = self._post(endpoint, json=payload, timeout=self._timeout)
        else:
            response = httpx.post(endpoint, json=payload, timeout=self._timeout)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("MuseTalk runtime returned an invalid payload")
        video_b64 = body.get("video_base64")
        if not isinstance(video_b64, str) or not video_b64:
            raise RuntimeError("MuseTalk runtime returned no video")

        video_bytes = base64.b64decode(video_b64, validate=True)
        if not video_bytes:
            raise RuntimeError("MuseTalk runtime returned empty video")

        actual_sha256 = hashlib.sha256(video_bytes).hexdigest()
        returned_sha = body.get("sha256")
        if returned_sha and returned_sha != actual_sha256:
            raise RuntimeError("MuseTalk runtime video sha256 mismatch")
        returned_audio_sha = body.get("audio_sha256")
        if returned_audio_sha and returned_audio_sha != audio_sha256:
            raise RuntimeError("MuseTalk runtime audio_sha256 does not match submitted audio")
        returned_portrait_sha = body.get("portrait_sha256")
        if returned_portrait_sha and returned_portrait_sha != portrait_sha256:
            raise RuntimeError("MuseTalk runtime portrait_sha256 does not match submitted portrait")

        mime_type = media_integrity.verify_media_bytes(video_bytes, ".mp4")
        filename = Path(str(body.get("filename") or f"lipsync-{actual_sha256[:12]}.mp4")).name
        if not filename.lower().endswith(".mp4"):
            filename = f"{Path(filename).stem}.mp4"

        contract = {
            "format": body.get("format") or LIPSYNC_CONTRACT_FORMAT,
            "provider": self.provider,
            "engine": body.get("engine") or "musetalk",
            "license": body.get("license") or "MIT",
            "mime_type": mime_type,
            "filename": filename,
            "size": len(video_bytes),
            "sha256": actual_sha256,
            "audio_sha256": audio_sha256,
            "portrait_sha256": portrait_sha256,
            "fps": int(body.get("fps") or fps),
            "bbox_shift": int(body.get("bbox_shift") if body.get("bbox_shift") is not None else bbox_shift),
            "character_id": character_id,
        }
        return video_bytes, contract

    def animate(
        self,
        portrait_path: Path,
        audio_path: Path,
        output: Path,
        *,
        fps: int = 25,
        bbox_shift: int = 0,
        character_id: str | None = None,
    ) -> dict:
        portrait = Path(portrait_path)
        audio = Path(audio_path)
        if not portrait.is_file():
            raise FileNotFoundError(f"Portrait file not found: {portrait}")
        if not audio.is_file():
            raise FileNotFoundError(f"Audio file not found: {audio}")
        video_bytes, contract = self.animate_bytes(
            portrait.read_bytes(),
            audio.read_bytes(),
            portrait_filename=portrait.name,
            audio_filename=audio.name,
            fps=fps,
            bbox_shift=bbox_shift,
            character_id=character_id,
        )
        out = Path(output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(video_bytes)
        contract["output_path"] = str(out)
        return contract
