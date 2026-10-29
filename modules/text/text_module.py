"""Text Module API — обгортка над чистою бізнес-логікою та провайдерами.

Реалізує Module API: ``execute``, ``cancel``, ``health_check``, ``resource_query``.
Виклики генерації (LLM) робляться через провайдери, а не напряму з модуля.
"""

from __future__ import annotations

from typing import Any

from modules.text.text_logic import (
    build_text_prompt,
    default_script_scenes,
    extract_voiceover,
    normalize_script_data,
    validate_script_document,
)

logger = __import__("logging").getLogger("modules.text")


class TextModule:
    """Text Module: паралельне співіснення з Text Node через feature flag."""

    def __init__(self, provider: Any | None = None, executor=None) -> None:
        self.provider = provider
        self._bound_executor = executor
        self._active_tasks: dict[str, dict] = {}

    @property
    def name(self) -> str:
        return "text"

    def health_check(self) -> dict[str, Any]:
        """Повернути стан модуля (без викликів LLM)."""
        return {
            "module": "text",
            "status": "READY",
            "provider": bool(self.provider),
            "active_tasks": len(self._active_tasks),
        }

    def resource_query(self) -> dict[str, Any]:
        """Потреби ресурсів Text Module (CPU-only, без GPU)."""
        return {
            "module": "text",
            "min_vram_mb": 0,
            "cpu_cores": 1,
            "ram_mb": 512,
            "gpu_required": False,
        }

    def execute(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        """Виконати текстове завдання: побудувати сценарій через провайдера.

        Логіка нормалізації — чиста (``text_logic.py``), виклик LLM — через провайдера.
        """
        if self._bound_executor is not None:
            from modules.execution import execute_bound
            return execute_bound(self, task)
        task_type = task.get("task", "text")
        topic = task.get("topic") or task.get("prompt") or ""
        system_prompt = task.get("system_prompt", "")
        character = task.get("character")

        prompt = build_text_prompt(topic, system_prompt, character)

        if task_type == "script":
            if self.provider is None:
                raise RuntimeError("Text Module requires an LLM provider for script generation")
            result = self.provider.generate_script(topic=topic, system_prompt=system_prompt, character=character)
            normalized = normalize_script_data(result, topic)
            errors = validate_script_document(normalized)
            if errors:
                raise ValueError(f"Invalid script: {'; '.join(errors)}")
            task_id = str(task.get("task_id") or task.get("job_id") or "unknown")
            self._active_tasks[task_id] = {"type": "script", "topic": topic}
            return [{"filename": "script.json", "kind": "script",
                     "data": normalized}]

        if task_type == "text":
            self._active_tasks[str(task.get("task_id") or task.get("job_id") or "unknown")] = {"type": "text", "topic": topic}
            return [{"filename": "response.txt", "kind": "text",
                     "data": prompt}]

        if task_type == "storyboard":
            scenes = default_script_scenes(topic)
            task_id = str(task.get("task_id") or task.get("job_id") or "unknown")
            self._active_tasks[task_id] = {"type": "storyboard", "topic": topic, "scenes": len(scenes)}
            return [{"filename": "storyboard.json", "kind": "storyboard",
                     "data": {"title": topic, "scenes": scenes}}]

        raise ValueError(f"Unknown text task type: {task_type}")

    def cancel(self, task_id: str) -> bool:
        """Скасувати активне завдання за task_id."""
        if task_id in self._active_tasks:
            del self._active_tasks[task_id]
            return True
        return False
