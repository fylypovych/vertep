"""Відновлення незавершених jobs з знімка (checkpoint/resume).

Issue #186.x: confirmation/progress/guard state survive a service restart
because the durable files under ``<BACKUP_ROOT>/.restore-state`` are the source
of truth.  In-memory dicts are only a fast cache.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from .snapshot import Snapshot


class ResumeManager:
    """Відновлення стану jobs і task з знімка."""

    def __init__(self, storage: Any) -> None:
        self.storage = storage

    def restore_jobs(self, snapshot_id: str) -> list[dict[str, Any]]:
        """Відновити всі jobs зі знімка."""
        snapshot = self.storage.load_snapshot(snapshot_id)
        if not snapshot.verify():
            raise ValueError(f"Snapshot {snapshot_id} failed integrity check")
        return list(snapshot.jobs)

    def restore_tasks(self, snapshot_id: str, job_id: str | None = None) -> list[dict[str, Any]]:
        """Відновити tasks, опціонально відфільтровані за job ID."""
        snapshot = self.storage.load_snapshot(snapshot_id)
        if not snapshot.verify():
            raise ValueError(f"Snapshot {snapshot_id} failed integrity check")
        tasks = list(snapshot.tasks)
        if job_id is not None:
            tasks = [t for t in tasks if t.get("job_id") == job_id]
        return tasks

    def restore_artifacts(self, snapshot_id: str) -> list[dict[str, Any]]:
        """Відновити посилання на артефакти зі знімка."""
        snapshot = self.storage.load_snapshot(snapshot_id)
        if not snapshot.verify():
            raise ValueError(f"Snapshot {snapshot_id} failed integrity check")
        return list(snapshot.artifacts)

    def selective_restore(self, snapshot_id: str, job_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Вибіркове відновлення jobs за списком ID."""
        snapshot = self.storage.load_snapshot(snapshot_id)
        if not snapshot.verify():
            raise ValueError(f"Snapshot {snapshot_id} failed integrity check")
        wanted = set(job_ids)
        jobs = [j for j in snapshot.jobs if j.get("job_id") in wanted]
        tasks = [t for t in snapshot.tasks if t.get("job_id") in wanted]
        return {"jobs": jobs, "tasks": tasks}

    def verify_snapshot(self, snapshot_id: str) -> bool:
        """Перевірити цілісність знімка."""
        try:
            return self.storage.verify_chain(snapshot_id)
        except Exception:
            return False

    # ── Issue #300 (i.0.0.3.0): module-specific backup and workflow checkpoints ──

    def save_module_state(self, snapshot_id: str, module_name: str,
                          state: dict[str, Any]) -> None:
        """Save module-specific state into a snapshot.

        Modules can declare their own backup requirements (cached intermediate
        data, module state) and store them under their name in the snapshot.
        """
        snapshot = self.storage.load_snapshot(snapshot_id)
        if not snapshot.verify():
            raise ValueError(f"Snapshot {snapshot_id} failed integrity check")
        snapshot.module_state[module_name] = state
        snapshot.checksum = snapshot.compute_checksum()
        self.storage.save_snapshot(snapshot)

    def restore_module_state(self, snapshot_id: str, module_name: str) -> dict[str, Any] | None:
        """Restore module-specific state from a snapshot.

        Returns the module state dict or None if not present.
        """
        snapshot = self.storage.load_snapshot(snapshot_id)
        if not snapshot.verify():
            raise ValueError(f"Snapshot {snapshot_id} failed integrity check")
        return snapshot.module_state.get(module_name)

    def list_modules_with_state(self, snapshot_id: str) -> list[str]:
        """List module names that have state in a snapshot."""
        snapshot = self.storage.load_snapshot(snapshot_id)
        if not snapshot.verify():
            raise ValueError(f"Snapshot {snapshot_id} failed integrity check")
        return list(snapshot.module_state.keys())

    def save_workflow_checkpoint(self, snapshot_id: str, job_id: str,
                                  checkpoint: dict[str, Any]) -> None:
        """Save a workflow engine checkpoint into a snapshot.

        Enables Workflow Engine checkpoint/resume via backup/restore.
        """
        snapshot = self.storage.load_snapshot(snapshot_id)
        if not snapshot.verify():
            raise ValueError(f"Snapshot {snapshot_id} failed integrity check")
        snapshot.workflow_checkpoints[job_id] = checkpoint
        snapshot.checksum = snapshot.compute_checksum()
        self.storage.save_snapshot(snapshot)

    def restore_workflow_checkpoint(self, snapshot_id: str, job_id: str) -> dict[str, Any] | None:
        """Restore a workflow checkpoint from a snapshot.

        Returns the checkpoint dict or None if not present.
        """
        snapshot = self.storage.load_snapshot(snapshot_id)
        if not snapshot.verify():
            raise ValueError(f"Snapshot {snapshot_id} failed integrity check")
        return snapshot.workflow_checkpoints.get(job_id)

    def list_workflow_checkpoints(self, snapshot_id: str) -> list[str]:
        """List job IDs that have workflow checkpoints in a snapshot."""
        snapshot = self.storage.load_snapshot(snapshot_id)
        if not snapshot.verify():
            raise ValueError(f"Snapshot {snapshot_id} failed integrity check")
        return list(snapshot.workflow_checkpoints.keys())


# ── Issue #186.x: durable confirmation / progress / guard ──────────────────

_RESTORE_GUARD_NAME = "restore.guard"
_RESTORE_GUARD_TTL = 3600
_RESTORE_OPERATIONS_NAME = "operations.json"
_RESTORE_CONFIRMATIONS_NAME = "confirmations.json"


class RestoreState:
    """Durable restore state: operations, confirmations and a cross-process guard."""

    def __init__(self, storage: SnapshotStorage) -> None:
        self.storage = storage
        self.state_dir = storage.root / ".restore-state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._guard_lock = threading.Lock()
        self._guard_owners: dict[str, str] = {}

    def _read_json(self, name: str, default: Any) -> Any:
        try:
            return json.loads((self.state_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _write_json(self, name: str, payload: Any) -> None:
        target = self.state_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(target)

    def _load_operations(self) -> dict[str, dict]:
        data = self._read_json(_RESTORE_OPERATIONS_NAME, {})
        return data if isinstance(data, dict) else {}

    def _load_confirmations(self) -> dict[str, dict]:
        data = self._read_json(_RESTORE_CONFIRMATIONS_NAME, {})
        return data if isinstance(data, dict) else {}

    def write_operation(self, snapshot_id: str, entry: dict) -> None:
        with self._lock:
            operations = self._load_operations()
            operations[snapshot_id] = entry
            self._write_json(_RESTORE_OPERATIONS_NAME, operations)

    def load_operation(self, snapshot_id: str) -> dict | None:
        with self._lock:
            return self._load_operations().get(snapshot_id)

    def write_confirmation(self, snapshot_id: str, token: str) -> None:
        with self._lock:
            confirmations = self._load_confirmations()
            confirmations[snapshot_id] = {
                "token": token,
                "used": False,
                "created_at": time.time(),
            }
            self._write_json(_RESTORE_CONFIRMATIONS_NAME, confirmations)

    def consume_confirmation(self, snapshot_id: str, token: str) -> bool:
        """Single-use confirmation bound to one snapshot.  Durable across restarts."""
        with self._lock:
            confirmations = self._load_confirmations()
            record = confirmations.get(snapshot_id)
            if not record or record.get("used") or record.get("token") != token:
                return False
            record["used"] = True
            record["used_at"] = time.time()
            self._write_json(_RESTORE_CONFIRMATIONS_NAME, confirmations)
            return True

    def acquire_guard(self, snapshot_id: str) -> str | None:
        """Exclude competing restores until the mutation ends (Issue #186.2)."""
        path = self.state_dir / _RESTORE_GUARD_NAME
        payload = json.dumps({"snapshot_id": snapshot_id, "pid": os.getpid(),
                              "started_at": time.time()})
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            info = self._read_json(_RESTORE_GUARD_NAME, {})
            age = time.time() - float(info.get("started_at", 0) or 0)
            if age <= _RESTORE_GUARD_TTL:
                return None
            try:
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
        with self._guard_lock:
            self._guard_owners[str(path)] = payload
        return str(path)

    def release_guard(self, guard: str | None) -> None:
        if not guard:
            return
        with self._guard_lock:
            expected = self._guard_owners.pop(guard, None)
        if expected is None:
            return
        try:
            path = Path(guard)
            if path.read_text(encoding="utf-8") == expected:
                path.unlink()
        except OSError:
            pass
