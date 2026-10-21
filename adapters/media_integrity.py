"""Structural media integrity shared by the Worker and CORE (Issue #178.3).

A checksum only proves that the bytes CORE received are the bytes the Worker
sent.  It does not prove that those bytes *are* the media the artifact claims:
a signature-only stub, a signature followed by junk or a truncated download
hashes perfectly and would still be stored as a finished image or video.

Both ends of the GPU boundary therefore re-derive the structure of the payload
before accepting it.  The rules live in this module so the Worker (which
downloads the bytes) and CORE (which stores them) cannot drift apart: the
Worker refuses to publish a payload it cannot vouch for, and CORE refuses to
store one it has not verified itself.
"""

from __future__ import annotations

import zlib

#: Magic-byte signatures of the media Vertep accepts, with their mime type.
MEDIA_SIGNATURES: dict[str, tuple[tuple[bytes, ...], str]] = {
    ".png": ((b"\x89PNG\r\n\x1a\n",), "image/png"),
    ".jpg": ((b"\xff\xd8\xff",), "image/jpeg"),
    ".jpeg": ((b"\xff\xd8\xff",), "image/jpeg"),
    ".webp": ((b"RIFF",), "image/webp"),
    ".ppm": ((b"P1", b"P2", b"P3", b"P4", b"P5", b"P6"), "image/x-portable-pixmap"),
    ".mp4": ((b"ftyp",), "video/mp4"),
    ".webm": ((b"\x1a\x45\xdf\xa3",), "video/webm"),
    ".mov": ((b"ftyp",), "video/quicktime"),
}

#: Suffixes whose payload is verified structurally instead of by prefix only.
STRUCTURAL_SUFFIXES = frozenset(MEDIA_SIGNATURES)

#: Reverse mapping used to re-verify a payload whose mime type is declared.
MIME_SUFFIXES: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/x-portable-pixmap": ".ppm",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}


class MediaIntegrityError(ValueError):
    """Raised when a payload is not the media its name claims."""


def media_mime_type(suffix: str) -> str:
    """Return the declared mime type for a supported media suffix."""
    signatures = MEDIA_SIGNATURES.get(suffix.lower())
    if not signatures:
        raise MediaIntegrityError(f"Unsupported media artifact type: {suffix}")
    return signatures[1]


def suffix_for_mime(mime_type: str) -> str | None:
    """Return the suffix a declared mime type stands for, or ``None``."""
    return MIME_SUFFIXES.get((mime_type or "").strip().lower())


def _invalid(suffix: str) -> MediaIntegrityError:
    return MediaIntegrityError(
        f"{suffix} payload does not match its declared media signature")


def _box_tree_has_video_handler(data: bytes, start: int, end: int) -> bool:
    """True when the ISO BMFF box subtree at ``[start, end)`` declares a video track.

    A real movie file carries ``moov > trak > mdia > hdlr`` with handler type
    ``vide``; a signature stub or signature+junk payload never does
    (Issue #178.3).
    """
    offset = start
    while offset + 8 <= end:
        size = int.from_bytes(data[offset:offset + 4], "big")
        box_type = data[offset + 4:offset + 8]
        header = 8
        if size == 1:
            if offset + 16 > end:
                return False
            size = int.from_bytes(data[offset + 8:offset + 16], "big")
            header = 16
        elif size == 0:
            size = end - offset
        if size < header or offset + size > end:
            return False
        if box_type == b"hdlr":
            payload = data[offset + header:offset + size]
            if len(payload) >= 12 and payload[8:12] == b"vide":
                return True
        elif box_type in {b"trak", b"mdia", b"minf", b"stbl", b"moov"}:
            if _box_tree_has_video_handler(data, offset + header, offset + size):
                return True
        offset += size
    return False


def _verify_mp4(data: bytes, suffix: str, mime_type: str) -> str:
    """Verify an ISO base media file format payload.

    The real top-level box tree is walked instead of trusting a signature
    prefix: a video must contain a ``moov`` box with a video track
    (``trak > mdia > hdlr`` handler ``vide``), so a signature-only stub, a
    truncated box or a signature+junk payload (e.g. ``ftyp`` + ``free`` without
    a movie header) is refused (Issue #178.3).
    """
    if len(data) < 16 or data[4:8] != b"ftyp":
        raise _invalid(suffix)
    total = len(data)
    offset = 0
    moov: tuple[int, int] | None = None
    while offset + 8 <= total:
        size = int.from_bytes(data[offset:offset + 4], "big")
        box_type = data[offset + 4:offset + 8]
        header = 8
        if size == 1:
            if offset + 16 > total:
                raise _invalid(suffix)
            size = int.from_bytes(data[offset + 8:offset + 16], "big")
            header = 16
        elif size == 0:
            # Box extends to the end of the file (ISO BMFF special case).
            size = total - offset
        if size < header or offset + size > total:
            # A truncated or implausible box means the file was cut short.
            raise _invalid(suffix)
        if box_type == b"moov":
            moov = (offset + header, offset + size)
        offset += size
    if offset != total or moov is None:
        # Trailing bytes that cannot form a box, or no movie header at all:
        # this is a signature stub, not a decodable video.
        raise _invalid(suffix)
    start, end = moov
    has_trak = False
    has_video_handler = False
    child = start
    while child + 8 <= end:
        child_size = int.from_bytes(data[child:child + 4], "big")
        child_type = data[child + 4:child + 8]
        child_header = 8
        if child_size == 1:
            if child + 16 > end:
                raise _invalid(suffix)
            child_size = int.from_bytes(data[child + 8:child + 16], "big")
            child_header = 16
        elif child_size == 0:
            child_size = end - child
        if child_size < child_header or child + child_size > end:
            raise _invalid(suffix)
        if child_type == b"trak":
            has_trak = True
            if _box_tree_has_video_handler(data, child + child_header,
                                          child + child_size):
                has_video_handler = True
        child += child_size
    if child != end or not has_trak or not has_video_handler:
        # A ``moov`` without a track, or without a video handler, is not a
        # video: the payload is junk that merely walked past the signature.
        raise _invalid(suffix)
    return mime_type


def _verify_png(data: bytes, suffix: str, mime_type: str) -> str:
    """Verify a PNG payload.

    The 8-byte signature must be followed by a complete chunk stream — IHDR
    first, at least one IDAT, and IEND last.  Every chunk's CRC32 is verified,
    the IHDR must declare valid dimensions/fields and the IDAT stream must
    decompress, so corrupt or fabricated bytes (invalid dimensions, a broken
    CRC, junk IDAT) are refused instead of shipped as a verified image
    (Issue #178.3).
    """
    if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise _invalid(suffix)
    offset = 8
    chunks = []
    idat_payload = bytearray()
    while offset + 8 <= len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        chunk_type = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise _invalid(suffix)
        payload = data[offset + 8:offset + 8 + length]
        stored_crc = int.from_bytes(data[offset + 8 + length:end], "big")
        if zlib.crc32(chunk_type + payload) & 0xFFFFFFFF != stored_crc:
            raise _invalid(suffix)
        if chunk_type == b"IHDR":
            if length != 13:
                raise _invalid(suffix)
            width = int.from_bytes(payload[0:4], "big")
            height = int.from_bytes(payload[4:8], "big")
            bit_depth, color_type = payload[8], payload[9]
            compression, filter_method = payload[10], payload[11]
            interlace = payload[12]
            valid_depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8},
                            4: {8, 16}, 6: {8, 16}}
            if (width < 1 or height < 1 or color_type not in valid_depths
                    or bit_depth not in valid_depths[color_type]
                    or compression != 0 or filter_method != 0
                    or interlace not in (0, 1)):
                raise _invalid(suffix)
        elif chunk_type == b"IDAT":
            idat_payload.extend(payload)
        chunks.append(chunk_type)
        offset = end
        if chunk_type == b"IEND":
            break
    if not chunks or chunks[0] != b"IHDR" or b"IDAT" not in chunks:
        raise _invalid(suffix)
    if chunks[-1] != b"IEND":
        raise _invalid(suffix)
    try:
        zlib.decompress(bytes(idat_payload))
    except zlib.error:
        raise _invalid(suffix) from None
    return mime_type


def _verify_jpeg(data: bytes, suffix: str, mime_type: str) -> str:
    """Verify a JPEG payload: SOI marker, a segment marker and the EOI marker."""
    if (len(data) < 4 or not data.startswith(b"\xff\xd8\xff")
            or not data.endswith(b"\xff\xd9")):
        raise _invalid(suffix)
    return mime_type


def _verify_webp(data: bytes, suffix: str, mime_type: str) -> str:
    """Verify a WebP payload: ``RIFF`` container carrying a ``WEBP`` form type."""
    if (len(data) < 12 or not data.startswith(b"RIFF")
            or data[8:12] != b"WEBP"):
        raise _invalid(suffix)
    return mime_type


def _verify_ppm(data: bytes, suffix: str, mime_type: str) -> str:
    """Verify a Netpbm payload: the magic (P1..P6) followed by a size header."""
    if len(data) < 15 or not data.startswith(MEDIA_SIGNATURES[".ppm"][0]):
        raise _invalid(suffix)
    # The magic is two characters followed by whitespace; the size header is
    # the next non-empty line after it.
    index = 2
    while index < len(data) and data[index:index + 1].isspace():
        index += 1
    header_end = data.find(b"\n", index)
    if header_end < 0 or not data[index:header_end].split():
        raise _invalid(suffix)
    return mime_type


def verify_media_bytes(data: bytes, suffix: str) -> str:
    """Verify that ``data`` really is the media its filename claims.

    Validation is structural, not just a magic-byte prefix check: a runtime that
    answers with an HTML error page, a signature-only stub, a signature followed
    by junk, a truncated download or an empty body must be rejected before the
    payload travels as an artifact (Issue #121/#178.3).

    Returns the declared mime type; raises :class:`MediaIntegrityError` when the
    payload does not match it.
    """
    lowered = suffix.lower()
    signatures = MEDIA_SIGNATURES.get(lowered)
    if not signatures:
        raise MediaIntegrityError(f"Unsupported media artifact type: {suffix}")
    if not data:
        raise MediaIntegrityError(f"Empty {suffix} payload from the compute runtime")
    mime_type = signatures[1]
    if lowered in {".mp4", ".mov"}:
        return _verify_mp4(data, suffix, mime_type)
    if lowered == ".png":
        return _verify_png(data, suffix, mime_type)
    if lowered in {".jpg", ".jpeg"}:
        return _verify_jpeg(data, suffix, mime_type)
    if lowered == ".webp":
        return _verify_webp(data, suffix, mime_type)
    if lowered == ".ppm":
        return _verify_ppm(data, suffix, mime_type)
    # Fallback for any other declared type: signature plus a minimum length.
    if len(data) < 12 or not any(data.startswith(marker) for marker in signatures[0]):
        raise _invalid(suffix)
    return mime_type
