"""GPU Node role launcher — паралельне співіснення з Image Module.

Делегує виконання на Image Module коли увімкнено feature flag
``VERTEP_IMAGE_MODULE_ENABLED``, інакше — на оригінальний executor з
``worker.role_executor``.  Старий робочий шлях зберігається для rollback.
"""

from __future__ import annotations

import os
from typing import Any

from worker.role_executor import execute_role_task as _original_execute
from modules.image.image_module import ImageModule

_image_module: ImageModule | None = None


def _module_enabled() -> bool:
    return os.getenv("VERTEP_IMAGE_MODULE_ENABLED", "false").lower() in {"true", "1", "yes"}


def _get_module() -> ImageModule:
    global _image_module
    if _image_module is None:
        _image_module = ImageModule()
    return _image_module


def execute(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Виконати завдання ролі gpu через модуль або оригінальний executor."""
    if _module_enabled():
        return _get_module().execute(task)
    return _original_execute("gpu", task)


def cancel(task_id: str) -> bool:
    """Скасувати активне завдання модуля генерації зображень."""
    if _module_enabled():
        return _get_module().cancel(task_id)
    return False


def health_check() -> dict[str, Any]:
    """Стан ролі gpu (module або оригінальний шлях)."""
    if _module_enabled():
        return _get_module().health_check()
    return {"role": "gpu", "status": "READY", "path": "original_executor"}


def resource_query() -> dict[str, Any]:
    """Потреби ресурсів ролі gpu."""
    if _module_enabled():
        return _get_module().resource_query()
    return {"role": "gpu", "min_vram_mb": 6144, "gpu_required": True}
