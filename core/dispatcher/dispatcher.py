"""Dispatcher - task dispatching for Architecture 2.0.

Handles task assignment to workers based on availability, capabilities,
resources, and lease management. Replaces hardcoded routing with
configurable provider abstraction.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Set

from .protocol.task import TaskRequest, TaskResponse


class Dispatcher:
    """Dispatcher for assigning tasks to workers."""

    RUNTIME_READY_STATUSES = {"ONLINE", "READY", "FREE", "BUSY"}

    def __init__(self, config: Optional[dict] = None) -> None:
        self.config = config or {}
        self._node_registry: Dict[str, dict] = {}  # node_id -> worker info
        self._task_leases: Dict[str, dict] = {}  # task_id -> {worker_id, expires_at}
        self._node_capabilities: Dict[str, set] = {}  # node_id -> capabilities
        self._node_resources: Dict[str, dict] = {}  # node_id -> {vram_mb, cpu_cores, etc.}

    def register_node(self, node_id: str, capabilities: set, resources: dict, role: str) -> None:
        """Register a worker node with its capabilities and resources."""
        self._node_registry[node_id] = {
            "capabilities": capabilities,
            "resources": resources,
            "role": role,
            "last_heartbeat": datetime.now(timezone.utc),
        }
        self._node_capabilities[node_id] = capabilities
        self._node_resources[node_id] = resources

    def unregister_node(self, node_id: str) -> None:
        """Unregister a worker node."""
        self._node_registry.pop(node_id, None)
        self._node_capabilities.pop(node_id, None)
        self._node_resources.pop(node_id, None)
        # Revoke any active tasks
        self._revoke_node_tasks(node_id)

    def _revoke_node_tasks(self, node_id: str) -> None:
        """Revoke all tasks assigned to a node."""
        tasks_to_revoke = [
            task_id for task_id, lease in self._task_leases.items()
            if lease.get("worker_id") == node_id
        ]
        for task_id in tasks_to_revoke:
            self.revoke_task(task_id)

    def assign_task(self, task: TaskRequest) -> Optional[TaskResponse]:
        """Assign a task to the best available worker.

        Args:
            task: TaskRequest describing the task to assign

        Returns:
            TaskResponse with accepted worker, or None if no suitable worker
        """
        # Find candidates with required capabilities
        required_cap = task.capabilities_required or {task.task_type}
        candidates = self._find_candidates(required_cap, task)

        if not candidates:
            return TaskResponse(
                accepted=False,
                declined_reason="No worker with required capabilities available",
            )

        # Select best candidate (load-aware, locality, etc.)
        candidate = self._select_best_candidate(candidates, task)

        if not candidate:
            return TaskResponse(
                accepted=False,
                declined_reason="No suitable worker after filtering",
            )

        # Grant lease
        lease_duration = timedelta(
            minutes=self.config.get("lease_duration_minutes", 30)
        )
        expires_at = datetime.now(timezone.utc) + lease_duration

        self._task_leases[task.task_id] = {
            "worker_id": candidate["node_id"],
            "expires_at": expires_at,
            "task_id": task.task_id,
        }

        # Update node status
        self._node_registry[candidate["node_id"]]["last_heartbeat"] = datetime.now(timezone.utc)

        return TaskResponse(
            accepted=True,
            worker_id=candidate["node_id"],
            capabilities_matched=candidate["capabilities"],
        )

    def _find_candidates(
        self, required_cap: set, task: TaskRequest
    ) -> list[dict]:
        """Find worker candidates with required capabilities."""
        candidates = []

        for node_id, info in self._node_registry.items():
            capabilities = info["capabilities"]
            if not required_cap.issubset(capabilities):
                continue  # Missing required capabilities

            # Check runtime readiness
            if info["role"] not in self.RUNTIME_READY_STATUSES:
                continue

            # Check resource requirements
            resources = info["resources"]
            min_vram = getattr(task, "vram_mb", None) or self.config.get(
                "default_vram_mb", 4096
            )
            if resources.get("vram_mb", 0) < min_vram:
                continue  # Insufficient VRAM

            candidates.append({
                "node_id": node_id,
                "capabilities": capabilities,
                "resources": resources,
            })

        return candidates

    def _select_best_candidate(
        self, candidates: list[dict], task: TaskRequest
    ) -> Optional[dict]:
        """Select the best candidate from available candidates."""
        if not candidates:
            return None

        # Simple selection: first available candidate
        # In production would consider: load, locality, priority, VRAM, etc.
        return candidates[0]

    def revoke_task(self, task_id: str) -> bool:
        """Revoke/ cancel a task lease."""
        if task_id in self._task_leases:
            del self._task_leases[task_id]
            return True
        return False

    def query_worker_status(self, node_id: str) -> Optional[dict]:
        """Query the status of a worker node."""
        return self._node_registry.get(node_id)

    def heartbeat(self, node_id: str) -> None:
        """Process a heartbeat from a worker node."""
        if node_id in self._node_registry:
            self._node_registry[node_id]["last_heartbeat"] = datetime.now(timezone.utc)