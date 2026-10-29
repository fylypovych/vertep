"""Local GPU Provider - provider for local model execution.

Implements GPUProvider interface for local inference without external services.
Used in local-only mode and as fallback in routing.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from core.gpu.provider_interface import GPUProvider


class LocalModelProvider(GPUProvider):
    """Provider for local model execution (no external API calls).

    Used for:
    - Local-only mode (blocks external AI calls)
    - Fallback when external providers are unavailable
    - Development and testing environments
    """

    is_local = True

    def __init__(self, configured: bool = False, execute=None, cancel=None, resources=None) -> None:
        self._configured = configured
        self._execute = execute
        self._cancel = cancel
        self._resources = resources or {}

    @property
    def name(self) -> str:
        return "local_model"

    @property
    def capabilities(self) -> set[str]:
        return {"image_generation", "text_generation", "voice_synthesis"}

    @property
    def min_vram_mb(self) -> int:
        return 2048  # Minimum 2GB for local models

    @property
    def max_concurrent_tasks(self) -> int:
        return 2  # Conservative default

    @property
    def configured(self) -> bool:
        return self._configured

    def configure(self) -> None:
        """Mark provider as configured."""
        self._configured = True

    def query_capability(
        self, request: dict[str, Any]
    ) -> dict[str, Any]:
        """Query local model capability.

        Returns available status based on configuration.
        """
        if not self._configured or self._execute is None:
            return {"available": False, "details": "Local model not configured"}

        model = request.get("model", "")
        task_type = request.get("type", "")

        # Simple capability check
        supported_types = {"image_generation", "text_generation", "voice_synthesis"}
        if task_type not in supported_types:
            return {"available": False, "details": f"Unsupported task type: {task_type}"}

        return {
            "available": True,
            "details": f"Local model ready for {task_type}",
        }

    def submit_inference(self, task: dict[str, Any]) -> dict[str, Any]:
        """Submit inference to local model.

        Requires an explicitly configured executor and returns its real output.
        """
        task_id = task.get("task_id", "unknown")

        if not self._configured or self._execute is None:
            raise RuntimeError("Local model execution is not configured")
        return {"task_id": task_id, "status": "complete", "artifacts": self._execute(task)}

    def cancel_inference(self, task_id: str) -> dict[str, Any]:
        """Cancel a local inference task.

        Returns cancellation acknowledgment.
        """
        if self._cancel is None:
            return {"task_id": task_id, "cancelled": False, "cleanup": False}
        return self._cancel(task_id)

    def get_resource_usage(self) -> dict[str, Any]:
        """Get current VRAM/CPU usage for local models.

        Returns current allocation status.
        """
        if self._resources:
            return dict(self._resources)
        return {
            "vram_total_mb": self.min_vram_mb,
            "vram_used_mb": self.min_vram_mb if self._configured else 0,
            "cpu_total_cores": 4,
            "cpu_used_cores": 2 if self._configured else 0,
            "memory_used_mb": 512 if self._configured else 0,
            "temperature_celsius": None,
        }
