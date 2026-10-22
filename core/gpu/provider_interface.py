"""GPU Provider Interface - abstract base class for GPU providers.

Defines the contract for all GPU providers (Ollama, ComfyUI, local models, etc.).
Used by GPURouter for fallback routing and resource management.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class GPUProvider(ABC):
    """Abstract base class for GPU providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name identifier."""

    @property
    @abstractmethod
    def capabilities(self) -> set[str]:
        """Set of supported task types (image_generation, video_generation, etc.)."""

    @property
    @abstractmethod
    def min_vram_mb(self) -> int:
        """Minimum VRAM required in MB."""

    @property
    @abstractmethod
    def max_concurrent_tasks(self) -> int:
        """Maximum number of concurrent tasks."""

    @abstractmethod
    def query_capability(
        self, request: dict[str, Any]
    ) -> dict[str, Any]:
        """Query provider capability.

        Args:
            request: {type: "image_generation"|"video_generation",
                     model: str, params: dict}

        Returns:
            {available: bool, details: dict}
        """

    @abstractmethod
    def submit_inference(self, task: dict[str, Any]) -> dict[str, Any]:
        """Submit inference task.

        Args:
            task: {task_id, prompt, character_config, params}

        Returns:
            {task_id, status: "queued"|"running"|"complete"|"failed",
             result_ref: str, vram_used_mb: int}
        """

    @abstractmethod
    def cancel_inference(self, task_id: str) -> dict[str, Any]:
        """Cancel running inference.

        Args:
            task_id: Task identifier

        Returns:
            {task_id, cancelled: bool, cleanup: bool}
        """

    @abstractmethod
    def get_resource_usage(self) -> dict[str, Any]:
        """Get current VRAM/CPU usage.

        Returns:
            {vram_total_mb: int, vram_used_mb: int,
             cpu_total_cores: int, cpu_used_cores: int,
             memory_used_mb: int, temperature_celsius: int | none}
        """