import json


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
