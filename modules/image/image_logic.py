"""Чиста бізнес-логіка Image Module.

Не залежить від CORE, адаптерів або зовнішніх сервісів.  Лише валідація
завдань генерації зображення/відео та підготовка параметрів для GPU-провайдера.
"""

from __future__ import annotations

from typing import Any


def validate_image_task(task: dict[str, Any]) -> list[str]:
    """Перевірити завдання генерації зображення.  Повертає список помилок."""
    errors: list[str] = []
    topic = task.get("topic") or task.get("prompt") or ""
    if not topic or not str(topic).strip():
        errors.append("topic/prompt is required for image generation")
    workflow = task.get("workflow") or ""
    if not workflow:
        errors.append("workflow is required for image generation")
    return errors


def validate_video_task(task: dict[str, Any]) -> list[str]:
    """Перевірити завдання генерації відео.  Повертає список помилок."""
    errors: list[str] = []
    topic = task.get("topic") or task.get("prompt") or ""
    if not topic or not str(topic).strip():
        errors.append("topic/prompt is required for video generation")
    return errors


def normalize_media_artifact(filename: str, kind: str, data: bytes,
                             metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Збудувати канонічний артефакт медіа (без base64 — тільки структуру)."""
    return {
        "filename": filename,
        "kind": kind,
        "size": len(data),
        "metadata": metadata or {},
    }


def default_image_params(task: dict[str, Any]) -> dict[str, Any]:
    """Дефолтні параметри генерації зображення."""
    return {
        "workflow": task.get("workflow") or "workflows/image/demo.json",
        "topic": task.get("topic") or task.get("prompt") or "",
        "task_type": "image",
    }
