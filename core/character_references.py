"""Reference images attached to a character and used as the default likeness sample."""

import hashlib
import os
import re
from pathlib import Path

MAX_REFERENCES = 5
MAX_BYTES = 10 * 1024 * 1024
_SIGNATURES = ((b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"))
_SAFE_NAME = re.compile(r"[a-f0-9]{16}\.(png|jpg|webp)")


def _directory(character_id: str) -> Path:
    return Path(os.getenv("CHARACTERS_ROOT", "characters")) / character_id / "references"


def _suffix(data: bytes) -> str | None:
    for signature, suffix in _SIGNATURES:
        if data.startswith(signature):
            return suffix
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def list_references(character_id: str) -> list[dict]:
    directory = _directory(character_id)
    if not directory.is_dir():
        return []
    return [{"filename": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
             "size": path.stat().st_size}
            for path in sorted(directory.iterdir()) if _SAFE_NAME.fullmatch(path.name)]


def save_reference(character_id: str, data: bytes) -> dict:
    suffix = _suffix(data)
    if suffix is None:
        raise ValueError("Підтримуються лише зображення PNG, JPEG або WebP")
    if len(data) > MAX_BYTES:
        raise ValueError("Зображення більше за 10 МБ")
    digest = hashlib.sha256(data).hexdigest()
    name = f"{digest[:16]}{suffix}"
    directory = _directory(character_id)
    existing = {item["filename"] for item in list_references(character_id)}
    if name not in existing and len(existing) >= MAX_REFERENCES:
        raise ValueError(f"Можна прикріпити не більше {MAX_REFERENCES} зображень")
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".{name}.tmp"
    temporary.write_bytes(data)
    temporary.replace(directory / name)
    return {"filename": name, "sha256": digest, "size": len(data)}


def reference_path(character_id: str, filename: str) -> Path | None:
    if not _SAFE_NAME.fullmatch(filename):
        return None
    path = _directory(character_id) / filename
    return path if path.is_file() else None
