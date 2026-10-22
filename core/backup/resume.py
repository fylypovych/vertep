"""Відновлення незавершених jobs з знімка (checkpoint/resume)."""

from __future__ import annotations

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
            snapshot = self.storage.load_snapshot(snapshot_id)
            return snapshot.verify()
        except Exception:
            return False
