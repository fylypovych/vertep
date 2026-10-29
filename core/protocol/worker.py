"""Worker protocol handler - handles CORE↔Worker protocol implementation.

Processes task assignments, capability reports, heartbeats, and lease renewals.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

from .task import (
    TaskClaim,
    TaskComplete,
    TaskProgress,
    TaskRequest,
    TaskResponse,
    TaskCancel,
    compatible_protocol,
)


class WorkerProtocolHandler:
    """Handles protocol messages from CORE."""

    def __init__(self, worker_id: str, core_endpoint: str | None = None) -> None:
        self.worker_id = worker_id
        self.core_endpoint = core_endpoint
        self.current_task: Optional[TaskClaim] = None
        self.capabilities: set = set()
        self.resources: dict = {}
        self.heartbeat_interval = 30  # seconds
        self._last_heartbeat: datetime = datetime.now(timezone.utc)

    def handle_task_assignment(self, request: TaskRequest) -> TaskResponse:
        """Handle a task assignment from CORE.

        Should validate the task, check capabilities, and accept or decline.
        """
        if not compatible_protocol(request.protocol_version) or self.current_task is not None:
            return TaskResponse(accepted=False, declined_reason="Incompatible protocol or busy worker")
        # Check if worker has required capabilities
        required = request.capabilities_required or {request.task_type}
        if not required.issubset(self.capabilities):
            return TaskResponse(
                accepted=False,
                declined_reason=f"Worker {self.worker_id} missing capabilities: {required - self.capabilities}",
            )

        # Check resource requirements
        if self.resources.get("vram_mb", 0) < request.vram_mb:
            return TaskResponse(
                accepted=False,
                declined_reason=f"Insufficient VRAM: have {self.resources.get('vram_mb', 0)}, need {request.vram_mb}",
            )

        # Accept the task
        self.current_task = TaskClaim(
            task_id=request.task_id,
            worker_id=self.worker_id,
            lease_expires_at=datetime.now(timezone.utc)
            + timedelta(minutes=request.lease_duration_minutes),
            lease_id=request.lease_id,
        )

        return TaskResponse(
            accepted=True,
            worker_id=self.worker_id,
            capabilities_matched=required,
            lease_id=request.lease_id,
        )

    def handle_heartbeat(self, data: dict) -> None:
        """Process a heartbeat from the worker.

        Updates last heartbeat time and task lease if active.
        """
        self._last_heartbeat = datetime.now(timezone.utc)

        # Extend lease if have active task
        if self.current_task and self.current_task.lease_expires_at:
            now = datetime.now(timezone.utc)
            if self.current_task.lease_expires_at <= now:
                return
            if data.get("lease_id") == self.current_task.lease_id and data.get("lease_expires_at"):
                expires = datetime.fromisoformat(data["lease_expires_at"].replace("Z", "+00:00"))
                if expires.tzinfo is not None and expires > now:
                    self.current_task.lease_expires_at = expires

    def handle_progress(self, progress: TaskProgress) -> None:
        """Process progress update from worker on current task."""
        if self.current_task and self.current_task.task_id == progress.task_id:
            # Update progress tracking
            pass  # Would update internal state

    def handle_task_complete(self, complete: TaskComplete) -> None:
        """Process task completion from worker."""
        if self.current_task and self.current_task.task_id == complete.task_id and self.current_task.lease_id == complete.lease_id and complete.worker_id == self.worker_id and compatible_protocol(complete.protocol_version):
            self.current_task = None
            # Would notify CORE of completion

    def handle_task_cancel(self, cancel: TaskCancel) -> None:
        """Process task cancellation."""
        if self.current_task and self.current_task.task_id == cancel.task_id and self.current_task.lease_id == cancel.lease_id and compatible_protocol(cancel.protocol_version):
            self.current_task = None
            # Would clean up resources
