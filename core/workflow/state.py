"""Workflow state management.

Handles storage and recovery of workflow job state with file-based
checkpoint/resume support for backup/restore integration.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


class WorkflowState:
    """Manages workflow job state persistence.

    Supports file-based checkpoint/resume so workflow state can be
    snapshotted and restored as part of CORE backup/restore.
    """

    def __init__(self, storage_backend: str = "file", state_root: str | Path | None = None) -> None:
        self.storage_backend = storage_backend
        self.state_root = Path(state_root or os.getenv("WORKFLOW_STATE_ROOT", "workflow_state"))
        self.state_root.mkdir(parents=True, exist_ok=True)
        self._memory: Dict[str, Dict[str, Any]] = {}

    def _job_path(self, job_id: str) -> Path:
        return self.state_root / f"{job_id}.json"

    def save_job(self, job_id: str, state: Dict[str, Any]) -> None:
        """Save complete job state to file and memory."""
        state_with_ts = dict(state)
        state_with_ts["saved_at"] = datetime.now(timezone.utc).isoformat()
        self._memory[job_id] = state_with_ts
        path = self._job_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(state_with_ts, ensure_ascii=False, default=str), encoding="utf-8")
        temporary.replace(path)

    def load_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Load job state by ID from file or memory."""
        if job_id in self._memory:
            return self._memory[job_id]
        path = self._job_path(job_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._memory[job_id] = data
            return data
        except (OSError, ValueError):
            return None

    def update_task_progress(
        self, job_id: str, task_id: str, progress: Dict[str, Any]
    ) -> None:
        """Update progress for a specific task within a job."""
        job = self.load_job(job_id)
        if job is None:
            return
        tasks = job.get("tasks", {})
        if task_id in tasks:
            tasks[task_id].update(progress)
            self.save_job(job_id, job)

    def complete_task(
        self, job_id: str, task_id: str, result: Any = None
    ) -> None:
        """Mark a task as completed with optional result."""
        job = self.load_job(job_id)
        if job is None:
            return
        tasks = job.get("tasks", {})
        if task_id in tasks:
            tasks[task_id]["status"] = "COMPLETED"
            tasks[task_id]["progress_percent"] = 100
            if result is not None:
                tasks[task_id]["result"] = result
            self.save_job(job_id, job)

    def fail_task(
        self, job_id: str, task_id: str, error: str
    ) -> None:
        """Mark a task as failed with error message."""
        job = self.load_job(job_id)
        if job is None:
            return
        tasks = job.get("tasks", {})
        if task_id in tasks:
            tasks[task_id]["status"] = "FAILED"
            tasks[task_id]["error"] = error
            self.save_job(job_id, job)

    def pause_job(self, job_id: str) -> None:
        """Pause a running job."""
        job = self.load_job(job_id)
        if job is None:
            return
        job["status"] = "PAUSED"
        self.save_job(job_id, job)

    def resume_job(self, job_id: str) -> None:
        """Resume a paused job."""
        job = self.load_job(job_id)
        if job is None:
            return
        job["status"] = "IN_PROGRESS"
        self.save_job(job_id, job)

    def cancel_job(self, job_id: str) -> None:
        """Cancel a job."""
        job = self.load_job(job_id)
        if job is None:
            return
        job["status"] = "CANCELLED"
        self.save_job(job_id, job)

    def get_job_status(self, job_id: str) -> Dict[str, Any]:
        """Get current status of a job."""
        job = self.load_job(job_id)
        if job is None:
            return {
                "status": "UNKNOWN",
                "tasks": {},
                "progress": 0,
            }
        tasks = job.get("tasks", {})
        return {
            "status": job.get("status", "UNKNOWN"),
            "tasks": {k: {
                "status": v.get("status", "UNKNOWN"),
                "progress_percent": v.get("progress_percent", 0),
                "error": v.get("error"),
            } for k, v in tasks.items()},
            "progress": job.get("progress", 0),
        }

    def checkpoint(self, job_id: str) -> Dict[str, Any] | None:
        """Create a checkpoint of a job for backup.

        Returns a serializable checkpoint dict or None if job not found.
        """
        job = self.load_job(job_id)
        if job is None:
            return None
        return {
            "job_id": job_id,
            "checkpoint_at": datetime.now(timezone.utc).isoformat(),
            "state": job,
        }

    def restore_from_checkpoint(self, checkpoint: Dict[str, Any]) -> bool:
        """Restore a job from a checkpoint dict.

        Returns True if restoration succeeded.
        """
        job_id = checkpoint.get("job_id")
        state = checkpoint.get("state")
        if not job_id or state is None:
            return False
        self._memory[job_id] = state
        self.save_job(job_id, state)
        return True

    def list_checkpoints(self) -> list[str]:
        """List all job IDs that have checkpoints."""
        return [p.stem for p in self.state_root.glob("*.json")]
