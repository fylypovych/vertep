"""Зашифровані локальні snapshot для Backup Node."""

import base64
import hashlib
import json
import os
import re
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


@app.get("/snapshots")
def list_snapshots() -> dict:
    root = _backup_root()
    root.mkdir(parents=True, exist_ok=True)
    snapshots = []
    for path in sorted(root.glob("*.vtbackup")):
        receipt_path = path.with_suffix(".json")
        if receipt_path.exists():
            try:
                snapshots.append(json.loads(receipt_path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    return {"snapshots": snapshots}


@app.post("/snapshots/{snapshot_id}/restore")
def restore_snapshot(snapshot_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", snapshot_id):
        raise HTTPException(422, "Некоректний ідентифікатор snapshot")
    key = _key()
    root = _backup_root()
    source = root / f"{snapshot_id}.vtbackup"
    if not source.exists():
        raise HTTPException(404, "Snapshot не знайдено")
    restore_root = root / "restore"
    restore_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=restore_root) as temporary:
        encrypted = Path(temporary) / "snapshot.vtbackup"
        archive = Path(temporary) / "snapshot.tar.gz"
        decrypted = Path(temporary) / "snapshot.tar.gz"
        encrypted.write_bytes(source.read_bytes())
        digest = _decrypt(encrypted, decrypted, key)
        with tarfile.open(decrypted, "r:gz") as tar:
            tar.extractall(restore_root / snapshot_id)
    return {"snapshot_id": snapshot_id, "restored_to": str(restore_root / snapshot_id), "sha256": digest}


def _decrypt(source: Path, destination: Path, key: bytes) -> str:
    with source.open("rb") as input_file:
        magic = input_file.read(len(MAGIC))
        if magic != MAGIC:
            raise RuntimeError("Invalid backup magic")
        nonce = input_file.read(12)
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).decryptor()
        digest = hashlib.sha256()
        with destination.open("wb") as output:
            for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                decrypted = decryptor.update(chunk)
                output.write(decrypted)
                digest.update(decrypted)
            final = decryptor.finalize()
            output.write(final)
            digest.update(final)
            output.flush()
            os.fsync(output.fileno())
    return digest.hexdigest()
