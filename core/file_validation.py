import hashlib
import json

MEDIA_CONTRACT_FORMAT = "media_contract/v1"
IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/webp", "image/x-portable-pixmap"}
VIDEO_MIME_TYPES = {"video/mp4", "video/webm", "video/quicktime"}


def validate_media_contract(artifact: dict, data: bytes, *, expected: str) -> None:
    """Verify a worker-supplied media contract against the received bytes.

    A contract is optional, but when present it must be truthful: the declared
    size, checksum and media kind are re-derived by CORE so a truncated, swapped
    or mislabelled payload cannot be stored as a finished artifact.
    ``expected`` is either ``"image"`` or ``"video"``.
    """
    contract = artifact.get("contract") or {}
    if not contract:
        return
    if contract.get("format") != MEDIA_CONTRACT_FORMAT:
        raise ValueError(f"Unsupported artifact contract: {contract.get('format')}")
    if contract.get("size") != len(data):
        raise ValueError("Artifact size does not match its contract")
    if contract.get("sha256") != hashlib.sha256(data).hexdigest():
        raise ValueError("Artifact checksum does not match its contract")
    mime_type = contract.get("mime_type")
    if expected == "image" and mime_type not in IMAGE_MIME_TYPES:
        raise ValueError("Artifact contract declares a non-image payload")
    if expected == "video" and mime_type not in VIDEO_MIME_TYPES:
        raise ValueError("Artifact contract declares a non-video payload")


def validate_signature(data: bytes, suffix: str) -> None:
    suffix = suffix.lower()
    valid = True
    if suffix == ".png":
        valid = data.startswith(b"\x89PNG\r\n\x1a\n")
    elif suffix in {".jpg", ".jpeg"}:
        valid = data.startswith(b"\xff\xd8\xff")
    elif suffix == ".webp":
        valid = len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    elif suffix == ".ppm":
        valid = data.startswith((b"P3", b"P6"))
    elif suffix == ".wav":
        valid = len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    elif suffix == ".ogg":
        valid = data.startswith(b"OggS")
    elif suffix == ".mp3":
        valid = data.startswith(b"ID3") or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)
    elif suffix == ".aac":
        valid = len(data) > 1 and data[0] == 0xFF and (data[1] & 0xF6) == 0xF0
    elif suffix == ".m4a":
        valid = len(data) >= 12 and data[4:8] == b"ftyp"
    elif suffix in {".mp4", ".mov"}:
        valid = len(data) >= 12 and data[4:8] == b"ftyp"
    elif suffix == ".webm":
        valid = data.startswith(b"\x1aE\xdf\xa3")
    elif suffix == ".txt":
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            valid = False
    elif suffix == ".json":
        try:
            json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            valid = False
    if not valid:
        raise ValueError(f"File signature does not match {suffix or 'the declared type'}")
