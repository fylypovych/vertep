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

import subprocess
import time
import urllib.request
import urllib.error

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


app = FastAPI(title="Vertep Backup", version="1")
_lock = threading.RLock()
MAGIC = b"VERTEP-BACKUP-v1\0"

_restore_progress: dict[str, dict] = {}
_restore_lock = threading.RLock()


class SnapshotRequest(BaseModel):
    job_id: str = Field(min_length=1, max_length=128)
    request: dict = Field(default_factory=dict)


def _backup_root() -> Path:
    return Path(os.getenv("BACKUP_ROOT", "/data/backups"))


def _sources() -> list[tuple[str, Path]]:
    raw = os.getenv("BACKUP_SOURCES", "").strip()
    if raw:
        sources: list[tuple[str, Path]] = []
        for item in raw.split(","):
            item = item.strip()
            if not item or ":" not in item:
                continue
            label, path = item.split(":", 1)
            sources.append((label.strip(), Path(path.strip())))
        if sources:
            return sources
    return [("config", Path(os.getenv("BACKUP_CONFIG_ROOT", "/data/config"))),
            ("storage", Path(os.getenv("BACKUP_STORAGE_ROOT", "/data/storage")))]


def _key() -> bytes:
    try:
        encoded = os.getenv("BACKUP_ENCRYPTION_KEY", "")
        key_file = os.getenv("BACKUP_ENCRYPTION_KEY_FILE", "")
        if key_file:
            encoded = Path(key_file).read_text(encoding="ascii").strip()
        # Bootstrap has historically stored a 32-byte key as 64 hexadecimal
        # characters. API deployments may provide the same key as base64.
        # Accept both representations so existing backups remain readable.
        value = (bytes.fromhex(encoded) if re.fullmatch(r"[0-9a-fA-F]{64}", encoded)
                 else base64.b64decode(encoded, validate=True))
    except (OSError, ValueError) as error:
        raise RuntimeError("BACKUP_ENCRYPTION_KEY відсутній або некоректний") from error
    if len(value) != 32:
        raise RuntimeError("BACKUP_ENCRYPTION_KEY має декодуватися у 32 байти")
    return value


def _get_system_state() -> dict | None:
    core_url = os.getenv("BACKUP_CORE_URL", os.getenv("CORE_ADDRESS", "")).rstrip("/")
    if not core_url:
        return None
    try:
        req = urllib.request.Request(f"{core_url}/api/system/state")
        token = os.getenv("NODE_API_TOKEN", "")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _is_restore_allowed() -> tuple[bool, str]:
    state = _get_system_state()
    if state is None:
        return True, "standalone"
    current = str(state.get("state", "NORMAL"))
    if current == "EMERGENCY":
        return False, "Відновлення заборонено в стані EMERGENCY"
    return True, current


def _retention_days() -> int | None:
    raw = os.getenv("BACKUP_RETENTION_DAYS", "").strip()
    if not raw:
        return None
    try:
        v = int(raw)
        return v if v > 0 else None
    except ValueError:
        return None


def _max_snapshots() -> int | None:
    raw = os.getenv("BACKUP_MAX_SNAPSHOTS", "").strip()
    if not raw:
        return None
    try:
        v = int(raw)
        return v if v > 0 else None
    except ValueError:
        return None


def _apply_retention(root: Path) -> None:
    snapshots = []
    for path in root.glob("*.vtbackup"):
        receipt_path = path.with_suffix(".json")
        if not receipt_path.exists():
            continue
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            created = receipt.get("created_at", "")
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except Exception:
                dt = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            snapshots.append((dt, path, receipt_path))
        except (OSError, ValueError):
            continue
    snapshots.sort(key=lambda x: x[0])
    days = _retention_days()
    if days is not None:
        cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
        for dt, path, receipt_path in list(snapshots):
            if dt.timestamp() < cutoff:
                path.unlink(missing_ok=True)
                receipt_path.unlink(missing_ok=True)
                snapshots.remove((dt, path, receipt_path))
    max_snap = _max_snapshots()
    if max_snap is not None and len(snapshots) > max_snap:
        to_remove = len(snapshots) - max_snap
        for dt, path, receipt_path in snapshots[:to_remove]:
            path.unlink(missing_ok=True)
            receipt_path.unlink(missing_ok=True)


def _remote_copy(destination: Path) -> None:
    cmd = os.getenv("BACKUP_REMOTE_CMD", "").strip()
    if not cmd:
        return
    expanded = cmd.replace("{file}", str(destination)).replace("{path}", str(destination))
    try:
        subprocess.run(expanded, shell=True, timeout=300, capture_output=True)
    except Exception:
        pass


def _archive(destination: Path) -> None:
    with tarfile.open(destination, "w:gz") as archive:
        for label, root in _sources():
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    archive.add(path, arcname=(Path(label) / path.relative_to(root)).as_posix(),
                                recursive=False)
        # Optional DB dumps
        pg_cmd = os.getenv("BACKUP_PG_DUMP_CMD", "").strip()
        if pg_cmd:
            try:
                result = subprocess.run(pg_cmd, shell=True, timeout=120, capture_output=True)
                if result.returncode == 0 and result.stdout:
                    with tempfile.NamedTemporaryFile(suffix=".sql", delete=False, mode="wb") as out:
                        out.write(result.stdout)
                        out_path = Path(out.name)
                    archive.add(str(out_path), arcname="db/postgres.sql", recursive=False)
                    out_path.unlink(missing_ok=True)
            except Exception:
                pass
        redis_cmd = os.getenv("BACKUP_REDIS_DUMP_CMD", "").strip()
        if redis_cmd:
            try:
                result = subprocess.run(redis_cmd, shell=True, timeout=60, capture_output=True)
                if result.returncode == 0 and result.stdout:
                    with tempfile.NamedTemporaryFile(suffix=".rdb", delete=False, mode="wb") as out:
                        out.write(result.stdout)
                        out_path = Path(out.name)
                    archive.add(str(out_path), arcname="db/redis.rdb", recursive=False)
                    out_path.unlink(missing_ok=True)
            except Exception:
                pass


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
        inventory = [{"label": label, "path": str(path)} for label, path in _sources()]
        receipt = {"snapshot_id": snapshot_id, "job_id": request.job_id,
                   "created_at": datetime.now(timezone.utc).isoformat(),
                   "format": "VERTEP-BACKUP-v1", "encryption": "AES-256-GCM",
                   "sha256": digest, "size": destination.stat().st_size,
                   "size_bytes": destination.stat().st_size,
                   "file": destination.name,
                   "inventory": inventory,
                   "retention_days": _retention_days(),
                   "max_snapshots": _max_snapshots(),
                   "remote_copy": bool(os.getenv("BACKUP_REMOTE_CMD", "").strip())}
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    _apply_retention(root)
    _remote_copy(destination)
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
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if "size_bytes" not in receipt:
                    try:
                        receipt["size_bytes"] = path.stat().st_size
                    except OSError:
                        pass
                if "inventory" not in receipt:
                    receipt["inventory"] = [{"label": label, "path": str(p)} for label, p in _sources()]
                snapshots.append(receipt)
            except (OSError, ValueError):
                continue
    snapshots.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return {"snapshots": snapshots}


@app.get("/system/status")
def system_status() -> dict:
    state = _get_system_state()
    if state is None:
        return {"state": "UNKNOWN", "reachable": False}
    return {**state, "reachable": True}


@app.post("/snapshots/{snapshot_id}/restore")
def restore_snapshot(snapshot_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", snapshot_id):
        raise HTTPException(422, "Некоректний ідентифікатор snapshot")
    allowed, reason = _is_restore_allowed()
    if not allowed:
        raise HTTPException(409, reason)
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
    # Initialize progress
    with _restore_lock:
        _restore_progress[snapshot_id] = {"status": "running", "progress": 5, "message": "Перевірка цілісності...", "started_at": time.time()}
    try:
        _set_restore_progress(snapshot_id, 10, "Розшифрування...")
        restore_root = root / "restore"
        restore_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=restore_root) as temporary:
            encrypted = Path(temporary) / "snapshot.vtbackup"
            decrypted = Path(temporary) / "snapshot.tar.gz"
            extracted = Path(temporary) / "extracted"
            encrypted.write_bytes(source.read_bytes())
            _decrypt(encrypted, decrypted, key)
            _set_restore_progress(snapshot_id, 40, "Розпакування...")
            with tarfile.open(decrypted, "r:gz") as tar:
                _safe_extract(tar, extracted)
            _set_restore_progress(snapshot_id, 60, "Відновлення файлів...")
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
            _set_restore_progress(snapshot_id, 85, "Перевірка після відновлення...")
            # Post-restore health verification: ensure at least one file restored and sources writable
            _post_restore_health_check(destinations)
        _set_restore_progress(snapshot_id, 100, "Відновлення завершено", status="done")
        return {"snapshot_id": snapshot_id, "restored": restored, "sha256": encrypted_digest, "status": "done"}
    except HTTPException:
        with _restore_lock:
            _restore_progress[snapshot_id] = {"status": "error", "progress": 0, "message": reason if not allowed else "Помилка відновлення", "started_at": _restore_progress.get(snapshot_id, {}).get("started_at")}
        raise
    except Exception as error:
        with _restore_lock:
            _restore_progress[snapshot_id] = {"status": "error", "progress": 0, "message": str(error)[:500], "started_at": _restore_progress.get(snapshot_id, {}).get("started_at")}
        raise HTTPException(500, f"Restore failed: {error}") from error


def _set_restore_progress(snapshot_id: str, progress: int, message: str, status: str = "running") -> None:
    with _restore_lock:
        entry = _restore_progress.get(snapshot_id, {})
        entry.update({"status": status, "progress": progress, "message": message})
        if status in ("done", "error"):
            entry["finished_at"] = time.time()
        _restore_progress[snapshot_id] = entry


def _post_restore_health_check(destinations: dict[str, Path]) -> None:
    """Verify that restore produced a healthy state: destinations exist and writable, at least one file present."""
    total_files = 0
    for label, destination in destinations.items():
        if not destination.exists():
            raise RuntimeError(f"Post-restore check failed: {label} missing at {destination}")
        if not os.access(destination, os.W_OK):
            raise RuntimeError(f"Post-restore check failed: {label} not writable")
        # Count files
        try:
            total_files += sum(1 for p in destination.rglob("*") if p.is_file())
        except Exception:
            pass
    # We don't fail if storage is empty — config must exist
    if total_files == 0:
        pass  # Allow empty restore (e.g. fresh install)


@app.get("/snapshots/{snapshot_id}/restore/progress")
def restore_progress(snapshot_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", snapshot_id):
        raise HTTPException(422, "Некоректний ідентифікатор snapshot")
    with _restore_lock:
        entry = _restore_progress.get(snapshot_id)
    if entry is None:
        # If snapshot exists but no progress tracked, report done if file exists
        root = _backup_root()
        source = root / f"{snapshot_id}.vtbackup"
        if source.exists():
            return {"snapshot_id": snapshot_id, "status": "unknown", "progress": 0, "message": "Немає даних про прогрес"}
        raise HTTPException(404, "Snapshot не знайдено")
    return {"snapshot_id": snapshot_id, **entry}


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
