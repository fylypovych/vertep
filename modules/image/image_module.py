"""Image Module API — обгортка над чистою бізнес-логікою та GPU-провайдером.

Реалізує Module API: ``execute``, ``cancel``, ``health_check``, ``resource_query``.
Виклики ComfyUI робляться через провайдера, а не напряму з модуля.
"""

from __future__ import annotations

from typing import Any

from modules.image.image_logic import (
    default_image_params,
    normalize_media_artifact,
    validate_image_task,
    validate_video_task,
)

logger = __import__("logging").getLogger("modules.image")


class ImageModule:
    """Image Module: паралельне співіснення з GPU Node через feature flag."""

    def __init__(self, provider: Any | None = None, executor=None) -> None:
        self.provider = provider
        self._bound_executor = executor
        self._active_tasks: dict[str, dict] = {}

    @property
    def name(self) -> str:
        return "image"

    def health_check(self) -> dict[str, Any]:
        """Повернути стан модуля (без викликів GPU)."""
        return {
            "module": "image",
            "status": "READY",
            "provider": bool(self.provider),
            "active_tasks": len(self._active_tasks),
        }

    def resource_query(self) -> dict[str, Any]:
        """Потреби ресурсів Image Module (GPU залежить від провайдера)."""
        return {
            "module": "image",
            "min_vram_mb": 6144,
            "cpu_cores": 2,
            "ram_mb": 2048,
            "gpu_required": True,
        }

    def execute(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        """Виконати завдання генерації зображення/відео через провайдера."""
        if self._bound_executor is not None:
            from modules.execution import execute_bound
            return execute_bound(self, task)
        task_type = task.get("task", "image")
        task_id = str(task.get("task_id") or task.get("job_id") or "unknown")

        if task_type == "image":
            errors = validate_image_task(task)
            if errors:
                raise ValueError(f"Invalid image task: {'; '.join(errors)}")
            if self.provider is None:
                raise RuntimeError("Image Module requires a compute provider")
            params = default_image_params(task)
            data, filename, kind = self.provider.generate_output(
                params["workflow"], params["topic"], params["task_type"])
            if kind != "image":
                raise RuntimeError(f"Compute provider returned unexpected kind: {kind}")
            self._active_tasks[task_id] = {"type": "image", "topic": params["topic"]}
            return [normalize_media_artifact(filename, "image", data, params)]

        if task_type == "video":
            errors = validate_video_task(task)
            if errors:
                raise ValueError(f"Invalid video task: {'; '.join(errors)}")
            if self.provider is None:
                raise RuntimeError("Image Module requires a compute provider")
            data, filename, kind = self.provider.generate_output(
                task.get("workflow") or "workflows/video/demo.json",
                task.get("topic") or task.get("prompt") or "",
                "video")
            if kind != "video":
                raise RuntimeError(f"Compute provider returned unexpected kind: {kind}")
            self._active_tasks[task_id] = {"type": "video", "topic": task.get("topic") or ""}
            return [normalize_media_artifact(filename, "video", data, {"workflow": task.get("workflow")})]

        if task_type == "assembly":
            self._active_tasks[task_id] = {"type": "assembly", "topic": task.get("topic") or ""}
            return [{"filename": "assembly.json", "kind": "assembly",
                     "data": {"status": "queued", "task_id": task_id}}]

        raise ValueError(f"Unknown image task type: {task_type}")

    def cancel(self, task_id: str) -> bool:
        """Скасувати активне завдання за task_id."""
        if task_id in self._active_tasks:
            del self._active_tasks[task_id]
            return True
        if self.provider is not None and hasattr(self.provider, "cancel"):
            try:
                return self.provider.cancel()
            except Exception:
                return False
        return False
