"""Зашифровані локальні snapshot для Backup Node."""

import base64
import hashlib
import json
import os
import secrets
import tarfile
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


app = FastAPI(title="Vertep Backup", version="1")
_lock = threading.RLock()
MAGIC = b"VERTEP-BACKUP-v1\0"


class SnapshotRequest(BaseModel):
    job_id: str = Field(min_length=1, max_length=128)
    request: dict = Field(default_factory=dict)


def _backup_root() -> Path:
    return Path(os.getenv("BACKUP_ROOT", "/data/backups"))


def _sources() -> list[tuple[str, Path]]:
    return [("config", Path(os.getenv("BACKUP_CONFIG_ROOT", "/data/config"))),
            ("storage", Path(os.getenv("BACKUP_STORAGE_ROOT", "/data/storage")))]


def _key() -> bytes:
    try:
        value = base64.b64decode(os.environ["BACKUP_ENCRYPTION_KEY"], validate=True)
    except (KeyError, ValueError) as error:
        raise RuntimeError("BACKUP_ENCRYPTION_KEY відсутній або некоректний") from error
    if len(value) != 32:
        raise RuntimeError("BACKUP_ENCRYPTION_KEY має декодуватися у 32 байти")
    return value


def _archive(destination: Path) -> None:
    with tarfile.open(destination, "w:gz") as archive:
        for label, root in _sources():
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    archive.add(path, arcname=(Path(label) / path.relative_to(root)).as_posix(),
                                recursive=False)


def _encrypt(source: Path, destination: Path, key: bytes) -> str:
    nonce = secrets.token_bytes(12)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    digest = hashlib.sha256()
    with source.open("rb") as input_file, destination.open("wb") as output:
        output.write(MAGIC + nonce)
        digest.update(MAGIC + nonce)
        for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
            encrypted = encryptor.update(chunk)
            output.write(encrypted)
            digest.update(encrypted)
        final = encryptor.finalize()
        output.write(final + encryptor.tag)
        digest.update(final + encryptor.tag)
        output.flush()
        os.fsync(output.fileno())
    return digest.hexdigest()


@app.get("/health")
def health() -> dict:
    _key()
    root = _backup_root()
    root.mkdir(parents=True, exist_ok=True)
    if not os.access(root, os.W_OK):
        raise HTTPException(503, "Каталог резервних копій недоступний для запису")
    return {"status": "HEALTHY", "encryption": "AES-256-GCM"}


@app.post("/snapshots")
def snapshot(request: SnapshotRequest) -> dict:
    key = _key()
    root = _backup_root()
    root.mkdir(parents=True, exist_ok=True)
    snapshot_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(6)
    destination = root / f"{snapshot_id}.vtbackup"
    receipt_path = root / f"{snapshot_id}.json"
    with _lock, tempfile.TemporaryDirectory(dir=root) as temporary:
        archive = Path(temporary) / "snapshot.tar.gz"
        encrypted = Path(temporary) / "snapshot.vtbackup"
        _archive(archive)
        digest = _encrypt(archive, encrypted, key)
        encrypted.replace(destination)
        receipt = {"snapshot_id": snapshot_id, "job_id": request.job_id,
                   "created_at": datetime.now(timezone.utc).isoformat(),
                   "format": "VERTEP-BACKUP-v1", "encryption": "AES-256-GCM",
                   "sha256": digest, "size": destination.stat().st_size,
                   "file": destination.name}
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return receipt
