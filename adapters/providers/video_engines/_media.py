"""Shared media and path helpers of the video engines."""

from __future__ import annotations

import struct
import wave
from pathlib import Path


def _path(p) -> str | None:
    return str(p) if p else None


class _JsonResponse:
    """Minimal response stand-in for a reconciled submit (§9.8).

    Reconciliation answers from the runtime's durable submit record, not from a
    live upstream body, but the caller expects the same envelope it would get from
    a real submit.
    """

    status_code = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _paths(paths) -> list[str]:
    return [str(p) for p in (paths or [])]


def _image_dimensions(path: Path) -> tuple[int, int] | None:
    """Read pixel dimensions from the file header without extra dependencies."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(65536)
    except OSError:
        return None
    if header[:8] == b"\x89PNG\r\n\x1a\n" and header[12:16] == b"IHDR":
        return struct.unpack(">II", header[16:24])
    if header[:2] == b"BM" and len(header) >= 26:
        width, height = struct.unpack("<ii", header[18:26])
        return (abs(width), abs(height))
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        chunk = header[12:16]
        if chunk == b"VP8X":
            return (
                int.from_bytes(header[24:27], "little") + 1,
                int.from_bytes(header[27:30], "little") + 1,
            )
        return None
    if header[:2] == b"\xff\xd8":
        offset = 2
        while offset + 9 < len(header):
            if header[offset] != 0xFF:
                offset += 1
                continue
            marker = header[offset + 1]
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB}:
                height, width = struct.unpack(">HH", header[offset + 5:offset + 9])
                return (int(width), int(height))
            if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                offset += 2
                continue
            offset += 2 + int.from_bytes(header[offset + 2:offset + 4], "big")
        return None
    if header[:2] in {b"P6", b"P3"}:
        parts = header[2:64].split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            return (int(parts[0]), int(parts[1]))
    return None


def _container_readable(path: Path, suffix: str) -> bool:
    """Verify the file really carries a container header of the declared kind."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(32)
    except OSError:
        return False
    if suffix in {".mp4", ".mov"}:
        return len(header) >= 12 and header[4:8] == b"ftyp"
    if suffix in {".mkv", ".webm"}:
        return header[:4] == b"\x1a\x45\xdf\xa3"
    return False


def _wav_duration(path: Path) -> float | None:
    """Duration of a PCM WAV file, or ``None`` when it is not a readable WAV."""
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
    except (OSError, wave.Error):
        return None
    if not rate or frames <= 0:
        return None
    return frames / float(rate)
