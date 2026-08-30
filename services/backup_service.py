"""Зашифровані локальні snapshot для Backup Node."""

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
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
        encoded = os.getenv("BACKUP_ENCRYPTION_KEY", "")
        key_file = os.getenv("BACKUP_ENCRYPTION_KEY_FILE", "")
        if key_file:
            encoded = Path(key_file).read_text(encoding="ascii").strip()
        value = base64.b64decode(encoded, validate=True)
    except (OSError, ValueError) as error:
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
    receipt_path = source.with_suffix(".json")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HTTPException(409, "Receipt snapshot відсутній або пошкоджений") from error
    encrypted_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if not secrets.compare_digest(encrypted_digest, str(receipt.get("sha256", ""))):
        raise HTTPException(409, "Checksum snapshot не збігається")
    restore_root = root / "restore"
    restore_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=restore_root) as temporary:
        encrypted = Path(temporary) / "snapshot.vtbackup"
        decrypted = Path(temporary) / "snapshot.tar.gz"
        extracted = Path(temporary) / "extracted"
        encrypted.write_bytes(source.read_bytes())
        _decrypt(encrypted, decrypted, key)
        with tarfile.open(decrypted, "r:gz") as tar:
            _safe_extract(tar, extracted)
        restored = []
        destinations = dict(_sources())
        for label, destination in destinations.items():
            source_root = extracted / label
            if not source_root.is_dir():
                continue
            destination.mkdir(parents=True, exist_ok=True)
            for item in sorted(source_root.rglob("*")):
                relative = item.relative_to(source_root)
                target = destination / relative
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                elif item.is_file() and not item.is_symlink():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary_target = target.with_name(f".{target.name}.restore-{os.getpid()}")
                    shutil.copy2(item, temporary_target)
                    temporary_target.replace(target)
                    restored.append(f"{label}/{relative.as_posix()}")
    return {"snapshot_id": snapshot_id, "restored": restored, "sha256": encrypted_digest}


def _safe_extract(archive: tarfile.TarFile, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    for member in archive.getmembers():
        name = Path(member.name)
        target = (destination / name).resolve()
        if (name.is_absolute() or ".." in name.parts or target != root and root not in target.parents
                or member.issym() or member.islnk() or member.isdev()):
            raise RuntimeError(f"Unsafe backup member: {member.name}")
    archive.extractall(destination)


def _decrypt(source: Path, destination: Path, key: bytes) -> str:
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with source.open("rb") as input_file:
        magic = input_file.read(len(MAGIC))
        if magic != MAGIC:
            raise RuntimeError("Invalid backup magic")
        nonce = input_file.read(12)
        ciphertext_size = source.stat().st_size - len(MAGIC) - len(nonce) - 16
        if ciphertext_size < 0:
            raise RuntimeError("Backup payload is truncated")
        input_file.seek(-16, os.SEEK_END)
        tag = input_file.read(16)
        input_file.seek(len(MAGIC) + len(nonce))
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        with destination.open("wb") as output:
            remaining = ciphertext_size
            while remaining:
                chunk = input_file.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise RuntimeError("Backup ciphertext is truncated")
                remaining -= len(chunk)
                decrypted = decryptor.update(chunk)
                output.write(decrypted)
            final = decryptor.finalize()
            output.write(final)
            output.flush()
            os.fsync(output.fileno())
    return digest
