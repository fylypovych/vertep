"""Worker-side protocol handler - processes CORE protocol messages.

Implements the worker-side of the CORE↔Worker protocol for task management,
capability reporting, and heartbeat/lease handling.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

from core.protocol.task import (
    TaskClaim,
    TaskComplete,
    TaskProgress,
    TaskRequest,
    TaskResponse,
    TaskCancel,
    compatible_protocol,
)


class WorkerProtocolHandler:
    """Handles incoming protocol messages from CORE."""

    def __init__(self, worker_id: str, core_endpoint: str | None = None) -> None:
        self.worker_id = worker_id
        self.core_endpoint = core_endpoint
        self.current_task: Optional[TaskClaim] = None
        self.capabilities: set = set()
        self.resources: dict = {"vram_mb": 0, "cpu_cores": 0}
        self.heartbeat_interval = 30
        self._last_heartbeat: datetime = datetime.now(timezone.utc)

    def handle_task_assignment(self, request: TaskRequest) -> TaskResponse:
        """Handle task assignment from CORE.

        Validates capabilities and resources, then accepts or declines.
        """
        if not compatible_protocol(request.protocol_version):
            return TaskResponse(accepted=False, declined_reason="Incompatible protocol version")
        if self.current_task is not None:
            return TaskResponse(accepted=False, declined_reason="Worker already owns a task")
        required = self._get_required_capabilities(request)
        if not required.issubset(self.capabilities):
            return TaskResponse(
                accepted=False,
                declined_reason=f"Missing capabilities: {required - self.capabilities}",
            )

        if self.resources.get("vram_mb", 0) < request.vram_mb:
            return TaskResponse(
                accepted=False,
                declined_reason=f"Insufficient VRAM",
            )

        # Accept task
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

    def _get_required_capabilities(self, request: TaskRequest) -> set:
        """Get the required capabilities for a task request."""
        required = request.capabilities_required or set()
        if not required:
            required = {request.task_type}
        return required

    def handle_heartbeat(self, data: dict) -> None:
        """Process heartbeat data from CORE (or send to CORE)."""

        self._last_heartbeat = datetime.now(timezone.utc)

        # Extend task lease if active and close to expiry
        if self.current_task and self.current_task.lease_expires_at:
            now = datetime.now(timezone.utc)
            time_until_expiry = (self.current_task.lease_expires_at - now).total_seconds()
            # A heartbeat cannot grant its own lease. Only an explicit CORE
            # renewal of this same live lease may extend the deadline.
            if time_until_expiry <= 0:
                return
            if data.get("lease_id") == self.current_task.lease_id and data.get("lease_expires_at"):
                expires = datetime.fromisoformat(data["lease_expires_at"].replace("Z", "+00:00"))
                if expires.tzinfo is not None and expires > now:
                    self.current_task.lease_expires_at = expires

    def handle_progress(self, progress: TaskProgress) -> None:
        """Handle progress update from CORE on current task."""
        if self.current_task and self.current_task.task_id == progress.task_id:
            # Update internal progress tracking
            pass

    def handle_task_complete(self, complete: TaskComplete) -> None:
        """Handle task completion from CORE."""
        if self.current_task and self.current_task.task_id == complete.task_id and self.current_task.lease_id == complete.lease_id and complete.worker_id == self.worker_id and compatible_protocol(complete.protocol_version):
            self.current_task = None
            # Would signal CORE about completion

    def handle_task_cancel(self, cancel: TaskCancel) -> None:
        """Handle task cancellation from CORE."""
        if self.current_task and self.current_task.task_id == cancel.task_id and self.current_task.lease_id == cancel.lease_id and compatible_protocol(cancel.protocol_version):
            self.current_task = None
            # Would clean up resources
