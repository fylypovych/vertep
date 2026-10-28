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
from contextlib import asynccontextmanager

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


_PROJECT_ROOT = Path(__file__).resolve().parent.parent

@asynccontextmanager
async def _lifespan(_app):
    recover_interrupted_restore()
    yield


app = FastAPI(title="Vertep Backup", version="1", lifespan=_lifespan)
_lock = threading.RLock()
MAGIC = b"VERTEP-BACKUP-v1\0"

# Issue #136: confirmations/progress/operation state must survive a service
# restart. The in-memory dicts remain as a fast cache, but the durable files
# under BACKUP_RESTORE_STATE_DIR are the source of truth.
_restore_progress: dict[str, dict] = {}
_restore_lock = threading.RLock()

_restore_confirmations: dict[str, dict] = {}
_confirm_lock = threading.RLock()

_RESTORE_GUARD_NAME = "restore.guard"
_RESTORE_GUARD_TTL = 3600
# Owner registry: only guards this process actually wrote may be refreshed or
# removed by it (Issue #186.2 — a competing restore must keep its exclusion).
_guard_owners: dict[str, str] = {}
_guard_lock = threading.Lock()
_RESTORE_OPERATIONS_NAME = "operations.json"
_RESTORE_CONFIRMATIONS_NAME = "confirmations.json"



class SnapshotRequest(BaseModel):
    job_id: str = Field(min_length=1, max_length=128)
    request: dict = Field(default_factory=dict)


def _backup_root() -> Path:
    return Path(os.getenv("BACKUP_ROOT", str(_PROJECT_ROOT / "backups")))


def _restore_state_dir() -> Path:
    override = os.getenv("BACKUP_RESTORE_STATE_DIR", "").strip()
    return Path(override) if override else _backup_root() / ".restore-state"


def _read_json_file(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _atomic_write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _load_operations() -> dict[str, dict]:
    data = _read_json_file(_restore_state_dir() / _RESTORE_OPERATIONS_NAME, {})
    return data if isinstance(data, dict) else {}


def _load_confirmations() -> dict[str, dict]:
    data = _read_json_file(_restore_state_dir() / _RESTORE_CONFIRMATIONS_NAME, {})
    return data if isinstance(data, dict) else {}


def _write_operation(snapshot_id: str, entry: dict) -> None:
    with _restore_lock:
        operations = _load_operations()
        operations[snapshot_id] = entry
        _atomic_write_json(_restore_state_dir() / _RESTORE_OPERATIONS_NAME, operations)
        _restore_progress[snapshot_id] = entry


def _acquire_restore_guard(snapshot_id: str) -> str | None:
    """Cross-process exclusion of competing restores until the mutation ends."""
    path = _restore_state_dir() / _RESTORE_GUARD_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"snapshot_id": snapshot_id, "pid": os.getpid(),
                          "started_at": time.time()})
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        info = _read_json_file(path, {})
        import psutil
        owner = info.get("pid")
        operation = _load_operations().get(info.get("snapshot_id"), {})
        if ((isinstance(owner, int) and psutil.pid_exists(owner))
                or operation.get("status") in {"running", "error"}):
            # Elapsed TTL cannot steal an active or interrupted mutation.
            return None
        age = time.time() - float(info.get("started_at", 0) or 0)
        if age <= _RESTORE_GUARD_TTL:
            return None
        try:
            if _read_json_file(path, {}) != info:
                return None
            path.unlink()
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            return None
    except OSError:
        return None
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    with _guard_lock:
        _guard_owners[str(path)] = payload
    return str(path)


def _refresh_restore_guard() -> None:
    """Extend the TTL of a guard this process owns while the restore runs.

    A restore longer than the TTL must not become stealable mid-flight: the
    exclusion has to hold until the mutation ends (Issue #186.2).  A guard whose
    content no longer matches what this process wrote is left untouched.
    """
    with _guard_lock:
        owned = dict(_guard_owners)
    for guard, expected in owned.items():
        try:
            path = Path(guard)
            if path.read_text(encoding="utf-8") != expected:
                continue
            info = json.loads(expected)
            info["started_at"] = time.time()
            payload = json.dumps(info)
            temporary = path.with_name(f"{path.name}.refresh-{os.getpid()}")
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, path)
            with _guard_lock:
                _guard_owners[guard] = payload
        except (OSError, ValueError, TypeError):
            continue


def _release_restore_guard(guard: str | None) -> None:
    if not guard:
        return
    with _guard_lock:
        expected = _guard_owners.pop(guard, None)
    if expected is None:
        # Not a guard this process wrote (anymore): it belongs to a competing
        # restore and must survive, however stale it looks from here.
        return
    try:
        path = Path(guard)
        # Only remove the guard while its content is exactly what this restore
        # wrote. A takeover by a competing restore must not be undone here
        # (Issue #186.2).
        if path.read_text(encoding="utf-8") == expected:
            path.unlink()
    except OSError:
        pass


def _finalize_restore_failure(snapshot_id: str, message: str, emergency_reason: str) -> None:
    """Persist an explicit failure result, recording whether EMERGENCY was confirmed."""
    emergency_confirmed = False
    emergency_error = None
    try:
        _set_emergency(emergency_reason)
        emergency_confirmed = True
    except Exception as error:  # noqa: BLE001 - must not hide the original failure
        emergency_error = str(error)[:500]
    previous = _restore_progress.get(snapshot_id) or _load_operations().get(snapshot_id, {})
    _write_operation(snapshot_id, {
        "status": "error",
        "progress": 0,
        "message": message,
        "started_at": previous.get("started_at"),
        "finished_at": time.time(),
        "emergency_confirmed": emergency_confirmed,
        "emergency_error": emergency_error,
    })


def recover_interrupted_restore() -> None:
    """Persist a crashed owner's failure; retain its fence for safe recovery."""
    import psutil
    guard = _read_json_file(_restore_state_dir() / _RESTORE_GUARD_NAME, {})
    snapshot_id = guard.get("snapshot_id")
    pid = guard.get("pid")
    if not snapshot_id or not isinstance(pid, int) or psutil.pid_exists(pid):
        return
    operation = _load_operations().get(snapshot_id, {})
    if operation.get("status") == "running":
        _finalize_restore_failure(snapshot_id, "Restore process interrupted",
                                  "Restore owner exited before completion")


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
        token = os.getenv("INTERNAL_API_KEY", "")
        if token:
            req.add_header("x-vertep-internal-key", token)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _is_restore_allowed() -> tuple[bool, str]:
    state = _get_system_state()
    if state is None:
        # Issue #36/#16/#60: an unreachable CORE state API must NOT be treated as implicit
        # confirmation that a restore is safe. With no CORE configured this is a
        # standalone node (restore proceeds); with a configured CORE that is
        # unreachable the safe-restore confirmation cannot be proved, so we deny.
        core_url = os.getenv("BACKUP_CORE_URL", os.getenv("CORE_ADDRESS", "")).rstrip("/")
        if not core_url:
            return True, "standalone"
        return False, "CORE state API недоступний; безпечне відновлення не підтверджено"
    current = str(state.get("state", "NORMAL"))
    # Issue #60: restore is only safe in MAINTENANCE or RECOVERING states.
    # NORMAL and UPDATING indicate active system usage — restoring there risks
    # data corruption and concurrent-write conflicts.
    _ALLOWED_STATES = {"MAINTENANCE", "RECOVERING"}
    if current not in _ALLOWED_STATES:
        return False, (
            f"Відновлення заборонено в стані {current}. "
            f"Дозволено лише в станах: {', '.join(sorted(_ALLOWED_STATES))}"
        )
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


def _remote_copy(destination: Path) -> bool:
    """Copy a snapshot to a remote policy store, returning whether it happened.

    Issue #36/#16: the previous implementation ignored the exit code and swallowed
    exceptions, so a failed remote copy still produced a receipt claiming success.
    Now a configured remote copy that fails raises a hard error (surfacing the remote
    storage policy failure) instead of being silently reported as done.
    """
    cmd = os.getenv("BACKUP_REMOTE_CMD", "").strip()
    if not cmd:
        return False
    expanded = cmd.replace("{file}", str(destination)).replace("{path}", str(destination))
    try:
        result = subprocess.run(expanded, shell=True, timeout=300, capture_output=True)
    except Exception as error:
        raise RuntimeError(f"BACKUP_REMOTE_CMD failed to run: {error}") from error
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode(errors="replace").strip()
        raise RuntimeError(
            f"BACKUP_REMOTE_CMD failed (exit {result.returncode}): {stderr or 'no stderr'}"
        )
    return True


def _pg_dump_path() -> str:
    """Target of the PostgreSQL dump. The appliance writes to /tmp; on Windows
    that path resolves to the drive root, so the dump stays in the project."""
    override = os.getenv("BACKUP_PG_DUMP_PATH", "").strip()
    if override:
        return override
    if os.name == "nt":
        return str(_PROJECT_ROOT / "var" / "tmp" / "vertep.dump")
    return "/tmp/vertep.dump"


def _pg_dump_default() -> str | None:
    return ("pg_dump -h ${POSTGRES_HOST:-postgres} -p ${POSTGRES_PORT:-5432} -U ${POSTGRES_USER:-vertep}"
            " -d ${POSTGRES_DB:-vertep} -Fc -f " + _pg_dump_path())


def _redis_dump_default() -> str | None:
    redis_url = os.getenv("REDIS_URL", "redis://:${REDIS_PASSWORD}@redis:6379/0")
    return f"redis-cli -u {redis_url} BGSAVE"


def _redis_dump_path() -> str:
    override = os.getenv("BACKUP_REDIS_DUMP_PATH", "").strip()
    if override:
        return override
    if os.name == "nt":
        return str(_PROJECT_ROOT / "var" / "lib" / "redis" / "dump.rdb")
    return "/var/lib/redis/dump.rdb"


def _command_exists(command: str) -> bool:
    import shutil
    return shutil.which(command.split()[0]) is not None


def _archive(destination: Path) -> None:
    with tarfile.open(destination, "w:gz") as archive:
        for label, root in _sources():
            if not root.exists():
                continue
            # Issue #60: preserve empty directories explicitly so they survive round-trip.
            # tarfile.add(recursive=False) only adds the exact path; rglob("*") skips dirs
            # that contain no files, so we add them as explicit TarInfo entries.
            seen_dirs: set[str] = set()
            for path in sorted(root.rglob("*")):
                arcname_base = (Path(label) / path.relative_to(root)).as_posix()
                if path.is_dir() and not path.is_symlink():
                    if arcname_base not in seen_dirs:
                        info = tarfile.TarInfo(name=arcname_base)
                        info.type = tarfile.DIRTYPE
                        info.mode = 0o755
                        archive.addfile(info)
                        seen_dirs.add(arcname_base)
                elif path.is_file() and not path.is_symlink():
                    archive.add(path, arcname=arcname_base, recursive=False)
            # Issue #92: if a source root is completely empty, add an empty directory
            # entry so the snapshot preserves the exact structure.
            if not seen_dirs:
                info = tarfile.TarInfo(name=f"{label}/")
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
        # Issue #60: pg_dump and redis-cli failures must be hard errors — a backup that
        # claims to include database state but silently skipped the dump is misleading.
        pg_cmd = os.getenv("BACKUP_PG_DUMP_CMD", _pg_dump_default() or "").strip()
        if pg_cmd:
            try:
                result = subprocess.run(pg_cmd, shell=True, timeout=120, capture_output=True)
            except Exception as exc:
                raise RuntimeError(f"pg_dump failed to run: {exc}") from exc
            if result.returncode != 0:
                stderr = (result.stderr or b"").decode(errors="replace").strip()
                raise RuntimeError(
                    f"pg_dump exited {result.returncode}: {stderr or 'no stderr'}"
                )
            dump_path = Path(_pg_dump_path())
            if not dump_path.exists():
                raise RuntimeError("pg_dump succeeded but dump file was not created")
            archive.add(str(dump_path), arcname="db/postgres.dump", recursive=False)
            dump_path.unlink(missing_ok=True)
        redis_cmd = os.getenv("BACKUP_REDIS_DUMP_CMD", _redis_dump_default() or "").strip()
        if redis_cmd:
            try:
                result = subprocess.run(redis_cmd, shell=True, timeout=60, capture_output=True)
            except Exception as exc:
                raise RuntimeError(f"redis-cli BGSAVE failed to run: {exc}") from exc
            if result.returncode != 0:
                stderr = (result.stderr or b"").decode(errors="replace").strip()
                raise RuntimeError(
                    f"redis-cli BGSAVE exited {result.returncode}: {stderr or 'no stderr'}"
                )
            rdb_path = Path(_redis_dump_path())
            if not rdb_path.exists():
                raise RuntimeError("redis-cli BGSAVE succeeded but RDB file was not created")
            archive.add(str(rdb_path), arcname="db/redis.rdb", recursive=False)


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
    try:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=root) as probe:
            probe.write(b"backup-health\n")
            probe.flush()
    except OSError as error:
        raise HTTPException(503, "Каталог резервних копій недоступний для запису") from error
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
        # Issue #36/#16: run remote copy inside the snapshot flow and record its REAL
        # outcome in the receipt. A configured remote copy that fails raises and the
        # snapshot is not reported as successful.
        remote_copied = _remote_copy(destination)
        receipt = {"snapshot_id": snapshot_id, "job_id": request.job_id,
                   "created_at": datetime.now(timezone.utc).isoformat(),
                   "format": "VERTEP-BACKUP-v1", "encryption": "AES-256-GCM",
                   "sha256": digest, "size": destination.stat().st_size,
                   "size_bytes": destination.stat().st_size,
                   "file": destination.name,
                   "inventory": inventory,
                   "retention_days": _retention_days(),
                   "max_snapshots": _max_snapshots(),
                   "remote_copy": remote_copied}
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    _apply_retention(root)
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


@app.get("/snapshots/{snapshot_id}/restore/progress")
def get_restore_progress(snapshot_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", snapshot_id):
        raise HTTPException(422, "Некоректний ідентифікатор snapshot")
    entry = _restore_progress.get(snapshot_id)
    if entry is None:
        entry = _load_operations().get(snapshot_id)
    if entry is None:
        return {"status": "unknown", "progress": 0, "message": ""}
    return {"status": entry.get("status", "unknown"), "progress": entry.get("progress", 0), "message": entry.get("message", "")}


@app.post("/snapshots/{snapshot_id}/restore")
def restore_snapshot(snapshot_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", snapshot_id):
        raise HTTPException(422, "Некоректний ідентифікатор snapshot")
    
    allowed, reason = _is_restore_allowed()
    if not allowed:
        raise HTTPException(409, reason)
        
    source = _backup_root() / f"{snapshot_id}.vtbackup"
    if not source.exists():
        raise HTTPException(404, "Snapshot не знайдено")
    
    token = secrets.token_hex(16)
    now = time.time()
    with _confirm_lock:
        # Prune expired/consumed tokens so the durable store does not grow forever.
        confirmations = {t: entry for t, entry in _load_confirmations().items()
                         if entry.get("status") == "pending" and entry.get("expires_at", 0) > now}
        confirmations[token] = {"snapshot_id": snapshot_id, "created_at": now,
                                "expires_at": now + 300, "status": "pending"}
        _atomic_write_json(_restore_state_dir() / _RESTORE_CONFIRMATIONS_NAME, confirmations)
        _restore_confirmations[token] = confirmations[token]

    return {
        "status": "confirmation_required",
        "confirmation_token": token,
        "message": "Відновлення потребує підтвердження. Використайте /confirm endpoint."
    }


@app.post("/snapshots/{snapshot_id}/restore/confirm")
def confirm_restore(snapshot_id: str, token: str) -> dict:
    if not token:
        raise HTTPException(401, "Невірний або відсутній токен підтвердження")
    now = time.time()
    with _confirm_lock:
        confirmations = _load_confirmations()
        conf = confirmations.get(token) or _restore_confirmations.get(token)
        if not conf or conf.get("snapshot_id") != snapshot_id or now > conf.get("expires_at", 0):
            raise HTTPException(401, "Токен підтвердження застарів або не відповідає snapshot")
        if conf.get("status") != "pending":
            # Issue #136: single-use token; a repeat must not start a second restore.
            raise HTTPException(401, "Токен підтвердження вже використано")

    return _execute_restore(snapshot_id, token)


def _consume_confirmation(token: str | None, snapshot_id: str) -> bool:
    """Durably mark a confirmation token used, once a restore is actually starting."""
    if not token:
        return True
    with _confirm_lock:
        confirmations = _load_confirmations()
        conf = confirmations.get(token)
        if (not conf or conf.get("status") != "pending" or conf.get("snapshot_id") != snapshot_id
                or time.time() > conf.get("expires_at", 0)):
            return False
        conf = {**conf, "status": "consumed", "consumed_at": time.time()}
        confirmations[token] = conf
        _atomic_write_json(_restore_state_dir() / _RESTORE_CONFIRMATIONS_NAME, confirmations)
        _restore_confirmations[token] = conf
        return True

def _execute_restore(snapshot_id: str, token: str | None = None) -> dict:
    key = _key()
    root = _backup_root()
    source = root / f"{snapshot_id}.vtbackup"
    receipt_path = source.with_suffix(".json")
    
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HTTPException(409, "Receipt snapshot відсутній або пошкоджений") from error
        
    encrypted_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if not secrets.compare_digest(encrypted_digest, str(receipt.get("sha256", ""))):
        raise HTTPException(409, "Checksum snapshot не збігається")

    # Issue #137: the receipt must describe this exact snapshot (format,
    # identity, integrity size) before any restore work starts.
    if str(receipt.get("format", "")) != "VERTEP-BACKUP-v1":
        raise HTTPException(409, "Формат snapshot не підтримується")
    if receipt.get("snapshot_id") != snapshot_id:
        raise HTTPException(409, "Receipt належить іншому snapshot")
    if receipt.get("file") not in (None, source.name):
        raise HTTPException(409, "Receipt описує інший файл snapshot")
    if receipt.get("encryption") not in (None, "AES-256-GCM"):
        raise HTTPException(409, "Receipt містить невідомий алгоритм шифрування")
    declared_size = receipt.get("size_bytes", receipt.get("size"))
    if declared_size is not None:
        try:
            declared = int(declared_size)
        except (TypeError, ValueError) as error:
            raise HTTPException(409, "Receipt містить некоректний розмір snapshot") from error
        if declared != source.stat().st_size:
            raise HTTPException(409, "Розмір snapshot не збігається з receipt")

    # Issue #136: the safe-state gate is re-checked at confirm time, so a state
    # change between the challenge and the confirmation cannot start a restore;
    # the token is not consumed here and the operator may retry.
    allowed, reason = _is_restore_allowed()
    if not allowed:
        raise HTTPException(409, reason)

    # Issue #136: exclude a competing restore (process or thread) before any mutation.
    guard = _acquire_restore_guard(snapshot_id)
    if guard is None:
        raise HTTPException(409, "Відновлення вже виконується іншим процесом")
    # Issue #136: consume the single-use token only once the restore really starts.
    if not _consume_confirmation(token, snapshot_id):
        _release_restore_guard(guard)
        raise HTTPException(401, "Токен підтвердження вже використано або застарів")

    started_at = time.time()
    _write_operation(snapshot_id, {"status": "running", "progress": 5,
                                   "message": "Перевірка цілісності...",
                                   "started_at": started_at,
                                   "operation_id": secrets.token_hex(8),
                                   "snapshot_id": snapshot_id})
        
    try:
        # Issue #60: System must be in MAINTENANCE/RECOVERING during restore
        _set_system_state("MAINTENANCE", f"System restore initiated for {snapshot_id}",
                          operation_id=f"restore:{snapshot_id}")
        
        if not _core_available():
            raise HTTPException(503, "CORE недоступний, відновлення заборонено")
            
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
                
            _set_restore_progress(snapshot_id, 60, "Очищення старих файлів...")
            destinations = dict(_sources())
            for label, destination in destinations.items():
                source_root = extracted / label
                if not source_root.is_dir():
                    continue
                destination.mkdir(parents=True, exist_ok=True)
                # A snapshot may contain an older global state. Never replace
                # the live restore fence with that historical NORMAL state.
                protected = {path for path in destination.rglob("system-state.json")
                             if _read_json_file(path, {}).get("operation_id") == f"restore:{snapshot_id}"}
                snapshot_files = set(protected)
                for item in sorted(source_root.rglob("*")):
                    relative = item.relative_to(source_root)
                    target = destination / relative
                    if target in protected:
                        continue
                    if item.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        snapshot_files.add(target)
                    elif item.is_file() and not item.is_symlink():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        temporary_target = target.with_name(f".{target.name}.restore-{os.getpid()}")
                        shutil.copy2(item, temporary_target)
                        temporary_target.replace(target)
                        snapshot_files.add(target)
                for existing in sorted(destination.rglob("*")):
                    if existing in snapshot_files:
                        continue
                    if existing.is_file() or (existing.is_symlink() and not existing.is_dir()):
                        existing.unlink(missing_ok=True)
                        
            _set_restore_progress(snapshot_id, 70, "Відновлення бази даних...")
            db_root = extracted / "db"
            if db_root.is_dir():
                pg_dump = db_root / "postgres.dump"
                if pg_dump.exists():
                    pg_restore = os.getenv("BACKUP_PG_RESTORE_CMD", f"pg_restore -h ${{POSTGRES_HOST:-postgres}} -p ${{POSTGRES_PORT:-5432}} -U ${{POSTGRES_USER:-vertep}} -d ${{POSTGRES_DB:-vertep}} -c {pg_dump}")
                    result = subprocess.run(pg_restore, shell=True, timeout=300, capture_output=True)
                    if result.returncode != 0:
                        raise RuntimeError(f"pg_restore failed: {result.stderr.decode()}")
                redis_rdb = db_root / "redis.rdb"
                if redis_rdb.exists():
                    redis_data = Path(os.getenv("REDIS_DATA_DIR", str(_redis_dump_path()))).parent
                    redis_data.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(redis_rdb, redis_data / "dump.rdb")
            
            _set_restore_progress(snapshot_id, 85, "Перевірка після відновлення...")
            _post_restore_health_check(destinations)

        # Issue #136: NORMAL is restored only after health verification passed.
        _set_system_state("NORMAL", f"System restored from {snapshot_id}")
        _set_restore_progress(snapshot_id, 100, "Відновлення завершено", status="done")
        return {"snapshot_id": snapshot_id, "restored": [], "sha256": encrypted_digest, "status": "done"}
        
    except HTTPException:
        _finalize_restore_failure(snapshot_id, "Помилка відновлення", "Restore failed")
        raise
    except Exception as error:
        _finalize_restore_failure(snapshot_id, str(error)[:500], f"Restore failed: {error}")
        raise HTTPException(500, f"Restore failed: {error}") from error
    finally:
        _release_restore_guard(guard)


def _set_restore_progress(snapshot_id: str, progress: int, message: str, status: str = "running") -> None:
    with _restore_lock:
        entry = dict(_restore_progress.get(snapshot_id) or _load_operations().get(snapshot_id, {}))
        entry.update({"status": status, "progress": progress, "message": message})
        if status in ("done", "error"):
            entry["finished_at"] = time.time()
        operations = _load_operations()
        operations[snapshot_id] = entry
        _atomic_write_json(_restore_state_dir() / _RESTORE_OPERATIONS_NAME, operations)
        _restore_progress[snapshot_id] = entry


def _post_restore_health_check(destinations: dict[str, Path]) -> None:
    """Issue #60: post-restore check must confirm CORE is HEALTHY, not just check directories.

    If a CORE URL is configured and CORE does not report HEALTHY/OK, the restore is
    considered failed and the caller is expected to call _set_emergency().
    """
    for label, destination in destinations.items():
        if not destination.exists():
            raise RuntimeError(f"Post-restore check failed: {label} missing at {destination}")
        if not os.access(destination, os.W_OK):
            raise RuntimeError(f"Post-restore check failed: {label} not writable")
    core_url = os.getenv("BACKUP_CORE_URL", os.getenv("CORE_ADDRESS", "")).rstrip("/")
    if core_url:
        health = _core_health_check()
        if health is None:
            raise RuntimeError(
                "Post-restore health check failed: CORE is unreachable after restore"
            )
        status = health.get("status", "")
        if status not in ("HEALTHY", "OK"):
            raise RuntimeError(
                f"Post-restore health check failed: CORE reported status={status!r}"
            )


def _core_available() -> bool:
    core_url = os.getenv("BACKUP_CORE_URL", os.getenv("CORE_ADDRESS", "")).rstrip("/")
    if not core_url:
        return True
    try:
        req = urllib.request.Request(f"{core_url}/api/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _set_system_state(state: str, reason: str, operation_id: str | None = None) -> None:
    """Set the GLOBAL system state on CORE."""
    core_url = os.getenv("BACKUP_CORE_URL", os.getenv("CORE_ADDRESS", "")).rstrip("/")
    if not core_url:
        return
    headers = {"Content-Type": "application/json"}
    token = os.getenv("INTERNAL_API_KEY", "")
    if token:
        headers["x-vertep-internal-key"] = token
    try:
        payload = json.dumps({"state": state, "reason": reason, "operation_id": operation_id}).encode("utf-8")
        req = urllib.request.Request(f"{core_url}/api/system/state", data=payload,
                                      headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except Exception as error:
        raise RuntimeError(f"Failed to set system state {state} via core API: {error}") from error


def _set_emergency(reason: str) -> None:
    core_url = os.getenv("BACKUP_CORE_URL", os.getenv("CORE_ADDRESS", "")).rstrip("/")
    if not core_url:
        raise RuntimeError("CORE is not configured; EMERGENCY cannot be confirmed")
    headers = {"Content-Type": "application/json"}
    token = os.getenv("INTERNAL_API_KEY", "")
    if token:
        headers["x-vertep-internal-key"] = token
    try:
        # Issue #36/#16: CORE did not expose a mutating state endpoint, so this POST
        # previously hit a GET-only route (405). The route now durably sets EMERGENCY.
        payload = json.dumps({"reason": reason, "operation_id": None}).encode("utf-8")
        req = urllib.request.Request(f"{core_url}/api/system/state", data=payload,
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except Exception as error:
        raise RuntimeError(f"Failed to set EMERGENCY via core API: {error}") from error
    # Verify the ACTUAL CORE state changed rather than trusting that the call succeeded.
    state = _get_system_state()
    if not state or state.get("state") != "EMERGENCY":
        raise RuntimeError("EMERGENCY state was not confirmed on CORE after restore failure")


def _core_health_check() -> dict | None:
    core_url = os.getenv("BACKUP_CORE_URL", os.getenv("CORE_ADDRESS", "")).rstrip("/")
    if not core_url:
        return None
    try:
        req = urllib.request.Request(f"{core_url}/api/health")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


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
