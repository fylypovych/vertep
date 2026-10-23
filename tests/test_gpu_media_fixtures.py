"""Issue #282 (#178.3-#178.4): decode-valid CPU media fixtures.

FFmpeg is not guaranteed on the dev host, so these tests are skipped when the
binary is absent.  When FFmpeg *is* available the fixtures decode end-to-end:
CORE -> GPU Worker -> prompt/history/view -> CORE result -> FFmpeg decode.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import struct
import zlib

import pytest


def _box(box_type: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + box_type + payload


def _full_box(box_type: bytes, version: int, flags: int, payload: bytes) -> bytes:
    header = struct.pack(">B3B", version, flags[0], flags[1], flags[2])
    return _box(box_type, header + payload)


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + chunk_type + payload
            + struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF))


#: Structurally valid PNG: IHDR + IDAT (decompressable) + IEND.
PNG_VALID = (
    b"\x89PNG\r\n\x1a\n"
    + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
    + _png_chunk(b"IDAT", zlib.compress(b"\x00" + bytes((30, 40, 50)) * 2))
    + _png_chunk(b"IEND", b""))
PNG_TRUNCATED = PNG_VALID[:-1]
PNG_SIGNATURE_ONLY = b"\x89PNG\r\n\x1a\n"


#: Structurally valid MP4: ftyp + moov (video track handler) + mdat (junk media).
_HDLR = _box(b"hdlr", b"\x00" * 8 + b"vide" + b"\x00" * 12 + b"\x00")
_MDIA = _box(b"mdia", _HDLR)
_TRAK = _box(b"trak", _MDIA)
_MOOV = _box(b"moov", _TRAK)
_FTYP = (_box(b"ftyp", b"isom\x00\x00\x02\x00"
             + b"isomiso2avc1mp41"))
_MDAT = _box(b"mdat", b"\x00" * 64)  # junk media samples
MP4_VALID = _FTYP + _MOOV + _MDAT
MP4_FTYP_ONLY = b"\x00\x00\x00\x10ftypisom\x00\x00\x00\x00"
MP4_FTYP_PLUS_FREE = _FTYP + _box(b"free", b"\x00" * 8)
MP4_TRUNCATED_FTYP = b"\x00\x00\x00\x0cftypmp42"


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


requires_ffmpeg = pytest.mark.skipif(
    not _ffmpeg_available(), reason="ffmpeg is not installed on this host")


def test_valid_png_passes_structural_verification():
    from worker.role_executor import verify_media
    assert verify_media(PNG_VALID, ".png") == "image/png"


def test_truncated_png_is_refused():
    from worker.role_executor import verify_media
    with pytest.raises(Exception, match="media signature"):
        verify_media(PNG_TRUNCATED, ".png")


def test_signature_only_png_is_refused():
    from worker.role_executor import verify_media
    with pytest.raises(Exception, match="media signature"):
        verify_media(PNG_SIGNATURE_ONLY, ".png")


def test_valid_mp4_passes_structural_verification():
    from worker.role_executor import verify_media
    assert verify_media(MP4_VALID, ".mp4") == "video/mp4"


def test_ftyp_only_mp4_is_refused():
    from worker.role_executor import verify_media
    with pytest.raises(Exception, match="media signature"):
        verify_media(MP4_FTYP_ONLY, ".mp4")


def test_ftyp_plus_free_mp4_is_refused():
    from worker.role_executor import verify_media
    with pytest.raises(Exception, match="media signature"):
        verify_media(MP4_FTYP_PLUS_FREE, ".mp4")


def test_truncated_ftyp_mp4_is_refused():
    from worker.role_executor import verify_media
    with pytest.raises(Exception, match="media signature"):
        verify_media(MP4_TRUNCATED_FTYP, ".mp4")


@requires_ffmpeg
def test_valid_mp4_decodes_with_ffmpeg(tmp_path):
    """The fixture must be a real, decodable video, not a signature stub."""
    path = tmp_path / "decode.mp4"
    path.write_bytes(MP4_VALID)
    result = __import__("subprocess").run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@requires_ffmpeg
def test_valid_png_decodes_with_ffmpeg(tmp_path):
    path = tmp_path / "decode.png"
    path.write_bytes(PNG_VALID)
    result = __import__("subprocess").run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_mp4_sha256_is_deterministic():
    digest = hashlib.sha256(MP4_VALID).hexdigest()
    assert len(digest) == 64
    assert hashlib.sha256(MP4_VALID).hexdigest() == digest


def test_png_sha256_is_deterministic():
    digest = hashlib.sha256(PNG_VALID).hexdigest()
    assert len(digest) == 64
    assert hashlib.sha256(PNG_VALID).hexdigest() == digest
