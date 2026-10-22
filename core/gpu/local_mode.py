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

    def __init__(self, configured: bool = False) -> None:
        self._configured = configured

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
        if not self._configured:
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

        In a full implementation, this would execute the actual model.
        For now, simulates task submission.
        """
        task_id = task.get("task_id", "unknown")
        task_type = task.get("type", "")

        # Simulate successful submission
        return {
            "task_id": task_id,
            "status": "queued",
            "result_ref": f"local://{task_id}/result",
            "vram_used_mb": self.min_vram_mb,
        }

    def cancel_inference(self, task_id: str) -> dict[str, Any]:
        """Cancel a local inference task.

        Returns cancellation acknowledgment.
        """
        return {
            "task_id": task_id,
            "cancelled": True,
            "cleanup": True,
        }

    def get_resource_usage(self) -> dict[str, Any]:
        """Get current VRAM/CPU usage for local models.

        Returns current allocation status.
        """
        return {
            "vram_total_mb": self.min_vram_mb,
            "vram_used_mb": self.min_vram_mb if self._configured else 0,
            "cpu_total_cores": 4,
            "cpu_used_cores": 2 if self._configured else 0,
            "memory_used_mb": 512 if self._configured else 0,
            "temperature_celsius": None,
        }